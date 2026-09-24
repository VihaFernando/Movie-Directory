import Hls from 'hls.js'
import { useCallback, useEffect, useRef, useState } from 'react'
import { fetchEmbed, fetchSubtitles } from '../api'

// How many times one title may re-resolve after its stream link expires.
// These tokens last ~4h but are bound to a playback session, so a long film
// can outlive more than one; the cap stops a genuinely dead title looping.
const MAX_STALE_RETRIES = 3

// Manages resolving a movie's stream and attaching it to a <video> element,
// including HLS quality levels and subtitle tracks (fetched separately from
// a dedicated subtitle CDN - the HLS manifests we've seen don't embed
// subtitle tracks themselves, see app/config.py SUBTITLE_URL_TEMPLATE).
export function usePlayer() {
  const [state, setState] = useState({ status: 'idle', slug: null, error: null, embed: null, posterUrl: null, season: null, episode: null, isRetry: false })
  const [levels, setLevels] = useState([]) // [{ index, height, bitrate }]
  const [currentLevel, setCurrentLevel] = useState(-1) // -1 = auto
  // Real measured bits/sec per level index, from actual downloaded segment
  // bytes (see FRAG_LOADED below) - NOT the manifest's BANDWIDTH field.
  // BANDWIDTH is defined by the HLS spec as the stream's PEAK bitrate, so
  // sizing off it overstates the real download by a wide margin (verified:
  // a real title's manifest reported up to ~6.6 Mbps for a level that
  // actually measured closer to half that in practice). This starts empty
  // per level and fills in as real segments for that level are downloaded,
  // so it is a measurement, not an upfront guess - the UI falls back to the
  // manifest bitrate (labeled as an estimate) only until this has data.
  const [measuredBitrates, setMeasuredBitrates] = useState({}) // { [levelIndex]: bitsPerSecond }
  // Raw accumulators behind measuredBitrates - kept out of state so every
  // segment doesn't trigger a render; measuredBitrates itself is only
  // updated (and re-renders) once every few seconds, batched below.
  const levelStatsRef = useRef({}) // { [levelIndex]: { bytes, seconds } }
  const [subtitleTracks, setSubtitleTracks] = useState([]) // [{ lang, url }]
  const [currentSubtitle, setCurrentSubtitle] = useState(-1) // -1 = off, else index into subtitleTracks
  const [buffering, setBuffering] = useState(false)
  const videoRef = useRef(null)
  const hlsRef = useRef(null)
  // Counts stale-link re-resolves for the current title. Bounded rather than
  // once-only: these tokens are short-lived, so a long film can legitimately
  // outlive more than one of them, and a single latch would strand playback
  // on the second expiry. The cap still stops a genuinely dead title from
  // looping forever.
  const retryCountRef = useRef(0)
  // Where playback had reached when a link expired, so the re-resolved
  // stream can pick up from there instead of restarting at 0:00.
  const resumeAtRef = useRef(0)
  // The last playhead position observed while playback was actually
  // running. Tracked continuously rather than read at error time: by the
  // moment an HLS error handler fires, open() has already run cleanup(),
  // which destroys the hls instance and remounts the <video> - so
  // videoRef.current.currentTime reads 0 there, and a stale-link recovery
  // would silently restart the film from the beginning ("re-resolving ...
  // at 0 s" in the console) instead of resuming where the viewer was.
  const lastTimeRef = useRef(0)
  // Bounds hls.js's in-place media-error recovery, which can otherwise be
  // attempted endlessly on genuinely broken media.
  const mediaRecoveryRef = useRef(0)
  // Everything the HLS error handler needs to re-resolve a stale link.
  // Deliberately a ref, not effect dependencies: this effect's cleanup calls
  // hls.destroy(), so anything in its dependency array that changes during
  // playback would tear down the player mid-stream and wipe the parsed
  // quality levels with it.
  const openArgsRef = useRef({ source: 'current', contentType: 'movie', slug: null, posterUrl: null, season: null, episode: null })
  // `open` is recreated on every render of this hook, so the error handler
  // reads it through a ref rather than closing over it.
  const openRef = useRef(null)

  const cleanup = useCallback(() => {
    if (hlsRef.current) {
      hlsRef.current.destroy() // stops segment fetching
      hlsRef.current = null
    }
    setLevels([])
    setCurrentLevel(-1)
    setSubtitleTracks([])
    setCurrentSubtitle(-1)
    setBuffering(false)
  }, [])

  const close = useCallback(() => {
    cleanup()
    setState({ status: 'idle', slug: null, error: null, embed: null, posterUrl: null, season: null, episode: null, isRetry: false })
  }, [cleanup])

  const open = useCallback(
    async (
      slug,
      posterUrl,
      source = 'current',
      contentType = 'movie',
      isRetry = false,
      season = null,
      episode = null,
      resumeSeconds = 0,
    ) => {
      cleanup()
      // A retry re-enters open() to mint a fresh link for the SAME title, so
      // it must keep the retry budget and the saved playhead. Only a genuine
      // user-initiated open resets them - zeroing unconditionally here would
      // restart the film at 0:00 and hand every retry a full budget again,
      // defeating both the resume and the loop guard.
      if (!isRetry) {
        retryCountRef.current = 0
        // A fresh open resuming a "Continue Watching" title seeds the same
        // resumeAtRef the stale-link retry path uses - MANIFEST_PARSED below
        // applies whatever is here to the playhead the moment the stream is
        // ready, regardless of which caller set it.
        resumeAtRef.current = resumeSeconds > 0 ? resumeSeconds : 0
        lastTimeRef.current = 0
        mediaRecoveryRef.current = 0
      }
      // Unlike the above, level measurements are reset on EVERY open() -
      // retry included. A retry mints a link from a fresh capture, which can
      // land on a different mirror host whose manifest lists its levels in a
      // different order - hls.js assigns level indices purely by array
      // position in whatever manifest it just parsed, with no identity that
      // carries across two separate fetches. So index 2 after a retry is not
      // guaranteed to be the same resolution as index 2 was before it, and
      // keeping old accumulated bytes keyed by the old indices would
      // silently attribute one level's real measurement to a different
      // level's row after any retry. Losing the pre-retry measurement is a
      // fair trade for never showing a wrong one.
      levelStatsRef.current = {}
      setMeasuredBitrates({})
      // A series always plays a specific episode, so default to S1E1 rather
      // than leaving these null. The backend would resolve the same episode
      // either way, but leaving them null client-side means the episode
      // panel has nothing to mark as currently playing.
      if (contentType === 'tv') {
        season = season ?? 1
        episode = episode ?? 1
      }
      openArgsRef.current = { source, contentType, slug, posterUrl, season, episode }
      // isRetry carried into state (not just used above) so WatchPage can
      // tell a re-resolve of the SAME title apart from a fresh open, and
      // keep <VideoPlayer> mounted through it - see WatchPage.jsx for why:
      // unmounting it mid-retry was tearing the fullscreen element out from
      // under the browser on titles whose capture is flaky enough to retry
      // often, breaking fullscreen's mouse tracking on exactly those titles.
      setState({ status: 'loading', slug, error: null, embed: null, posterUrl, season, episode, isRetry })
      try {
        // Subtitles are keyed by TMDB id, which every source's slugs already
        // are, so they resolve regardless of which source the stream came
        // from - this used to be gated to 'current' and silently hid working
        // subtitles for the others. The endpoint returns [] for a title with
        // none, and a failure here must not take playback down with it.
        const [embed, tracks] = await Promise.all([
          fetchEmbed(slug, source, contentType, season, episode),
          // Subtitles for a series are per-episode and live under their own
          // path on the CDN, so they need the same season/episode as the
          // stream - passing only the slug returned another title's file.
          fetchSubtitles(slug, contentType, season, episode).catch(() => []),
        ])
        setSubtitleTracks(tracks)
        setState((s) => ({ ...s, status: 'ready', embed }))
      } catch (err) {
        setState((s) => ({ ...s, status: 'error', error: err.message }))
      }
    },
    [cleanup],
  )

  // Switch the open player to another episode of the same series.
  //
  // Deliberately NOT a retry (isRetry stays false): a different episode is a
  // new title as far as playback is concerned, so it must start at 0:00 with
  // a fresh retry budget rather than inheriting the previous episode's saved
  // playhead - resuming episode 2 at episode 1's timestamp is exactly the
  // bug the resume logic would otherwise cause here.
  const playEpisode = useCallback(
    (season, episode) => {
      const { slug, posterUrl, source, contentType } = openArgsRef.current
      if (!slug) return
      open(slug, posterUrl, source, contentType, false, season, episode)
    },
    [open],
  )

  // Keep the ref pointing at the latest open() so the HLS error handler can
  // call it without the effect depending on open()'s identity. Assigned in
  // an effect rather than during render, which is the supported way to keep
  // a ref in sync with a value.
  useEffect(() => {
    openRef.current = open
  }, [open])

  const setQuality = useCallback((levelIndex) => {
    if (hlsRef.current) hlsRef.current.currentLevel = levelIndex
    setCurrentLevel(levelIndex)
  }, [])

  // Toggles native <video> textTracks (rendered via <track> elements in
  // VideoPlayer) rather than hls.js's subtitle API - these are separately
  // fetched .vtt files, not part of the HLS stream itself.
  const setSubtitle = useCallback((trackIndex) => {
    const video = videoRef.current
    if (video) {
      Array.from(video.textTracks).forEach((tt, i) => {
        tt.mode = i === trackIndex ? 'showing' : 'disabled'
      })
    }
    setCurrentSubtitle(trackIndex)
  }, [])

  // Attach hls.js (or rely on native HLS) once the <video> element for an
  // HLS title actually mounts - can't do this until state.status is
  // 'ready' and the ref exists, so this runs as an effect keyed on that.
  useEffect(() => {
    if (state.status !== 'ready' || !state.embed?.is_hls) return
    const video = videoRef.current
    if (!video) return

    // Prefer hls.js wherever it works, and fall back to native HLS only if
    // it doesn't (essentially just Safari).
    //
    // The order matters and used to be the other way round: canPlayType()
    // returns a truthy "maybe" for HLS in some Chromium builds, so the
    // native branch could win there and set video.src directly. That plays,
    // but it exposes no level API - so MANIFEST_PARSED never fires, `levels`
    // stays empty, and the quality menu (gated on levels.length > 1) never
    // renders, while subtitles keep working because they are separate
    // <track> elements. Checking Hls.isSupported() first keeps the quality
    // control available on every browser that can support it.
    if (!Hls.isSupported()) {
      if (video.canPlayType('application/vnd.apple.mpegurl')) {
        // Native HLS: playback works, but there is no per-level control
        // surface, so the quality menu is legitimately unavailable here.
        video.src = state.embed.proxied_url
        return
      }
      setState((s) => ({ ...s, status: 'error', error: 'HLS playback is not supported in this browser' }))
      return
    }
    const hls = new Hls({
      // Default is 0.1s, which is tight: a tab returning from the background
      // often parks a fraction of a second inside a gap, and too small a
      // tolerance leaves hls.js unsure whether it is really stuck.
      maxBufferHole: 0.5,
      // How long the element may sit not advancing before hls.js treats it
      // as stalled and nudges it itself. Our visibilitychange handler is the
      // backstop for when even this does not fire.
      highBufferWatchdogPeriod: 1,
      // Keep more decoded audio/video around so a backgrounded tab is less
      // likely to have its buffer evicted entirely while throttled.
      backBufferLength: 90,
    })
    hlsRef.current = hls

    hls.on(Hls.Events.ERROR, (evt, data) => {
      // These stream links carry a short-lived token, so one captured a
      // while ago (a stale tab, a title left open) is dead on arrival and
      // the proxy answers 410 Gone. Recover by re-resolving the title once.
      //
      // The trigger is the 410 itself, not hls.js's error taxonomy: it
      // retries a failing URL internally several times before escalating,
      // and by the time it gives up it may report the failure as a
      // mediaError rather than a networkError. Keying on the status code
      // catches the first response and doesn't depend on how the failure
      // is eventually classified - and because it fires before hls.js
      // exhausts its own retries, playback recovers faster too.
      const status = data.response?.code
      const { slug, posterUrl, source, contentType, season, episode } = openArgsRef.current
      const canRetry = retryCountRef.current < MAX_STALE_RETRIES && slug

      const reResolve = (why) => {
        retryCountRef.current += 1
        // Remember the playhead so the fresh stream resumes here rather than
        // restarting the film.
        // Prefer the live element if it still holds a sane position, but
        // fall back to the continuously-tracked value, which survives the
        // cleanup()/remount that precedes this handler.
        const live = videoRef.current?.currentTime || 0
        resumeAtRef.current = live > 1 ? live : lastTimeRef.current
        console.info(`${why} - re-resolving`, slug, 'at', Math.round(resumeAtRef.current), 's')
        openRef.current?.(slug, posterUrl, source, contentType, true, season, episode)
      }

      if (status === 410 && canRetry) {
        reResolve('Stream link expired')
        return
      }

      if (!data.fatal) return
      console.error('Fatal HLS error:', data.type, data.details)

      // A fatal network error with no 410 (the link died some other way):
      // still worth re-resolving before giving up on the title.
      if (data.type === Hls.ErrorTypes.NETWORK_ERROR && canRetry) {
        reResolve('Fatal network error')
        return
      }

      // A codec this browser's MediaSource cannot decode at all - not a
      // recoverable hiccup. recoverMediaError() re-attaches MediaSource and
      // re-parses the same manifest, which fails on the identical codec
      // again every time: retrying it just burns the retry budget while the
      // video sits frozen on the poster frame with a spinner forever,
      // which is what "the controls don't respond" turned out to actually
      // be (see the Spider-Verse report - the player wasn't unresponsive,
      // it was stuck retrying an error it could never recover from). Surface
      // this immediately instead.
      if (data.details === Hls.ErrorDetails.MANIFEST_INCOMPATIBLE_CODECS_ERROR) {
        setState((s) => ({
          ...s,
          status: 'error',
          error: 'This title\'s stream uses a video format your browser cannot play.',
        }))
        return
      }

      // Other fatal media errors are usually a decoder hiccup rather than a
      // dead link, and hls.js can often recover in place - try that before
      // surfacing an error the user can only respond to by reloading.
      if (data.type === Hls.ErrorTypes.MEDIA_ERROR && mediaRecoveryRef.current < 2) {
        mediaRecoveryRef.current += 1
        console.info('Fatal media error - attempting in-place recovery')
        hls.recoverMediaError()
        return
      }

      setState((s) => ({ ...s, status: 'error', error: `Playback error: ${data.details}` }))
    })
    hls.on(Hls.Events.MANIFEST_PARSED, (evt, data) => {
      setLevels(
        data.levels.map((lvl, i) => ({ index: i, height: lvl.height, bitrate: lvl.bitrate })),
      )
      // Restore the playhead after a stale-link re-resolve, so recovery is
      // invisible rather than throwing the viewer back to the start.
      const resumeAt = resumeAtRef.current
      if (resumeAt > 0 && videoRef.current) {
        videoRef.current.currentTime = resumeAt
        resumeAtRef.current = 0
      }
    })
    hls.on(Hls.Events.LEVEL_SWITCHED, (evt, data) => setCurrentLevel(data.level))

    // Accumulate real bytes/seconds per level as segments actually download,
    // for measuredBitrates - see its declaration above for why this exists
    // instead of trusting the manifest's BANDWIDTH field. frag.stats.total is
    // the fragment's real transferred byte count (hls.js measures this from
    // the actual network response, not the manifest); frag.duration is that
    // segment's real playback length.
    //
    // Flushed to React state on every one of a level's first few segments
    // (so opening the quality menu early still shows a real, if rough,
    // number rather than nothing) and then only every 5th segment after
    // that once the average has enough samples to be stable - flushing on
    // every segment for the whole film would mean a render per segment.
    const flushedCount = {} // { [levelIndex]: how many times we've set state for it }
    hls.on(Hls.Events.FRAG_LOADED, (evt, data) => {
      const { frag } = data
      const bytes = frag?.stats?.total
      const seconds = frag?.duration
      if (!bytes || !seconds || frag.level == null) return

      const stats = levelStatsRef.current
      const prev = stats[frag.level] || { bytes: 0, seconds: 0 }
      const nextCount = (prev.count || 0) + 1
      stats[frag.level] = { bytes: prev.bytes + bytes, seconds: prev.seconds + seconds, count: nextCount }

      const flushedFor = flushedCount[frag.level] || 0
      if (nextCount > 3 && nextCount - flushedFor < 5) return
      flushedCount[frag.level] = nextCount
      setMeasuredBitrates(
        Object.fromEntries(
          Object.entries(stats).map(([level, s]) => [level, (s.bytes * 8) / s.seconds]),
        ),
      )
    })

    // attachMedia() is asynchronous internally - loadSource() must wait
    // for MEDIA_ATTACHED rather than being called back-to-back with
    // attachMedia(), which is a race (the manifest sometimes never loads
    // at all if loadSource() is called before attachMedia() finishes).
    hls.once(Hls.Events.MEDIA_ATTACHED, () => hls.loadSource(state.embed.proxied_url))
    hls.attachMedia(video)
    return () => hls.destroy()
  }, [state.status, state.embed])

  // Buffering indicator: native <video>/media events work the same whether
  // hls.js or native HLS is driving playback.
  useEffect(() => {
    const video = videoRef.current
    if (!video || state.status !== 'ready') return
    const onWaiting = () => setBuffering(true)
    const onPlaying = () => setBuffering(false)
    const onCanPlay = () => setBuffering(false)
    // Keep the last real playhead position. This is what a stale-link
    // recovery resumes from - see lastTimeRef, which exists because the
    // error handler runs after the element has already been remounted and
    // reset to 0.
    const onTime = () => {
      const t = video.currentTime
      if (t > 0) lastTimeRef.current = t
    }
    video.addEventListener('waiting', onWaiting)
    video.addEventListener('playing', onPlaying)
    video.addEventListener('canplay', onCanPlay)
    video.addEventListener('timeupdate', onTime)
    return () => {
      video.removeEventListener('waiting', onWaiting)
      video.removeEventListener('playing', onPlaying)
      video.removeEventListener('canplay', onCanPlay)
      video.removeEventListener('timeupdate', onTime)
    }
  }, [state.status])

  // Recover the stall that happens when the tab is backgrounded for a while
  // and then returned to: video sits frozen on one frame while audio and
  // subtitles keep running.
  //
  // Cause: a hidden tab has its timers throttled and its video decoding
  // suspended, while the audio pipeline keeps going. hls.js's buffer can
  // then drift far behind (or be evicted entirely) and, once the tab is
  // visible again, its own gap-jumping does not always kick back in - the
  // media element stays parked at a currentTime with nothing buffered
  // around it.
  //
  // Fix, cheapest first: nudge the playhead to the start of the next
  // buffered range if there is one, otherwise ask hls.js to flush and
  // reload its buffer from the current position. Only ever runs when the
  // element really is stuck (visible, not paused, not advancing).
  useEffect(() => {
    if (state.status !== 'ready') return
    const video = videoRef.current
    if (!video) return

    const unstick = () => {
      if (document.hidden || video.paused || video.ended || video.seeking) return

      const now = video.currentTime
      // Give the browser a moment to resume on its own before intervening -
      // a tab that has just become visible usually recovers by itself, and
      // seeking unnecessarily would be a visible glitch.
      window.setTimeout(() => {
        if (document.hidden || video.paused || video.ended || video.seeking) return
        if (video.currentTime > now) return // resumed on its own

        const buffered = video.buffered
        for (let i = 0; i < buffered.length; i += 1) {
          // A range starting just ahead of us means the data is there but
          // the element is parked in a gap - jump the gap.
          if (buffered.start(i) > video.currentTime && buffered.start(i) - video.currentTime < 10) {
            console.info('Playback stalled after tab resume - skipping buffer gap')
            video.currentTime = buffered.start(i) + 0.1
            video.play().catch(() => {})
            return
          }
        }

        // Nothing usable buffered around the playhead: make hls.js refetch
        // from here. startLoad(currentTime) is the documented way to do that
        // and is far less disruptive than tearing the instance down.
        console.info('Playback stalled after tab resume - reloading buffer')
        hlsRef.current?.stopLoad()
        hlsRef.current?.startLoad(video.currentTime)
        video.play().catch(() => {})
      }, 1500)
    }

    const onVisibility = () => {
      if (!document.hidden) unstick()
    }

    document.addEventListener('visibilitychange', onVisibility)
    // 'stalled'/'waiting' also fire for ordinary rebuffering; unstick() only
    // acts if the playhead genuinely fails to advance, so it is safe here.
    video.addEventListener('stalled', unstick)
    return () => {
      document.removeEventListener('visibilitychange', onVisibility)
      video.removeEventListener('stalled', unstick)
    }
  }, [state.status])

  useEffect(() => () => cleanup(), [cleanup])

  return {
    ...state,
    videoRef,
    open,
    playEpisode,
    close,
    levels,
    currentLevel,
    setQuality,
    subtitleTracks,
    currentSubtitle,
    setSubtitle,
    buffering,
    measuredBitrates,
  }
}
