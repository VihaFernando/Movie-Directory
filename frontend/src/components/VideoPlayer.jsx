import { useEffect, useRef, useState } from 'react'
import { proxiedImageUrl } from '../api'
import { MAX_BOOST, useAudioBoost } from '../hooks/useAudioBoost'
import './VideoPlayer.css'

function formatTime(sec) {
  if (!Number.isFinite(sec)) return '0:00'
  const h = Math.floor(sec / 3600)
  const m = Math.floor((sec % 3600) / 60)
  const s = Math.floor(sec % 60)
  const mm = h > 0 ? String(m).padStart(2, '0') : m
  const ss = String(s).padStart(2, '0')
  return h > 0 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`
}

function bytesToLabel(bytes) {
  const mb = bytes / 1_000_000
  return mb >= 1000 ? `${(mb / 1000).toFixed(1)} GB` : `${Math.round(mb)} MB`
}

// Estimated download size for one quality level over the whole title.
//
// Prefers `measuredBitrate` - the REAL bits/sec measured from actual
// downloaded segment bytes for this level (see usePlayer.js's
// measuredBitrates/FRAG_LOADED) - over the manifest's advertised bitrate.
// That preference matters: HLS's BANDWIDTH tag is defined by spec as the
// stream's PEAK bitrate, not its average, so sizing off it alone
// systematically overstates the real download - verified against a live
// title where the manifest advertised ~6.6 Mbps for a level that measured
// well under that once actual segments were counted. Only once a level has
// no real measurement yet (nothing from it has downloaded this session) does
// this fall back to the manifest figure, clearly labeled "up to" since it is
// a ceiling rather than a real expectation.
function estimateSize(manifestBitrate, measuredBitrate, durationSec) {
  if (!Number.isFinite(durationSec) || durationSec <= 0) return null
  if (Number.isFinite(measuredBitrate) && measuredBitrate > 0) {
    return { label: `~${bytesToLabel((measuredBitrate * durationSec) / 8)}`, measured: true }
  }
  if (Number.isFinite(manifestBitrate) && manifestBitrate > 0) {
    return { label: `up to ${bytesToLabel((manifestBitrate * durationSec) / 8)}`, measured: false }
  }
  return null
}

export function VideoPlayer({ player, posterUrl, title, subtitle }) {
  const { videoRef, buffering, levels, currentLevel, setQuality, subtitleTracks, currentSubtitle, setSubtitle, subtitleOffset, setSubtitleOffset, measuredBitrates } = player
  const containerRef = useRef(null)
  const [playing, setPlaying] = useState(false)
  const [progress, setProgress] = useState(0)
  const [duration, setDuration] = useState(0)
  const [volume, setVolume] = useState(1)
  const [muted, setMuted] = useState(false)
  const [showControls, setShowControls] = useState(true)
  const [menu, setMenu] = useState(null) // 'quality' | 'subtitles' | 'audio' | null
  const [started, setStarted] = useState(false)
  const hideTimer = useRef(null)
  // Some sources are mastered well below the streaming norm; element.volume
  // can only attenuate, so lifting them needs a Web Audio gain stage.
  const { boost, setBoost, boostSupported } = useAudioBoost(videoRef)

  useEffect(() => {
    const video = videoRef.current
    if (!video) return
    const onPlay = () => setPlaying(true)
    const onPause = () => setPlaying(false)
    // Duration has to be re-read as playback proceeds, not captured once.
    //
    // With HLS the manifest's total length is often not known at
    // `loadedmetadata` - video.duration reads NaN at that point, and a
    // one-shot handler would latch that NaN forever. progressPct then
    // computes 0/NaN, so the seek bar stayed pinned at 0% for the whole
    // film even though the time readout (which uses currentTime directly)
    // looked correct. Re-checking on every timeupdate costs nothing and
    // picks the real duration up as soon as it becomes available.
    const readDuration = () => {
      const d = video.duration
      if (Number.isFinite(d) && d > 0) setDuration((prev) => (prev === d ? prev : d))
    }
    const onTime = () => {
      setProgress(video.currentTime)
      readDuration()
    }
    const onLoaded = readDuration
    const onFirstPlay = () => setStarted(true)
    // hls.js attaches media via MediaSource after the <video> mounts, so the
    // autoplay/autoPlay attribute (evaluated once on mount, before any
    // source exists) never actually triggers playback - starting play()
    // once enough data has buffered is the reliable way to autostart.
    const onCanPlay = () => video.play().catch(() => {})
    video.addEventListener('play', onPlay)
    video.addEventListener('pause', onPause)
    video.addEventListener('timeupdate', onTime)
    video.addEventListener('loadedmetadata', onLoaded)
    video.addEventListener('durationchange', onLoaded)
    video.addEventListener('playing', onFirstPlay)
    video.addEventListener('canplay', onCanPlay)
    return () => {
      video.removeEventListener('play', onPlay)
      video.removeEventListener('pause', onPause)
      video.removeEventListener('timeupdate', onTime)
      video.removeEventListener('loadedmetadata', onLoaded)
      video.removeEventListener('durationchange', onLoaded)
      video.removeEventListener('playing', onFirstPlay)
      video.removeEventListener('canplay', onCanPlay)
    }
  }, [videoRef])

  const togglePlay = () => {
    const v = videoRef.current
    if (!v) return
    if (v.paused) v.play()
    else v.pause()
  }

  const skip = (delta) => {
    const v = videoRef.current
    if (!v) return
    v.currentTime = Math.max(0, Math.min(duration, v.currentTime + delta))
  }

  const seekTo = (ratio) => {
    const v = videoRef.current
    if (!v || !duration) return
    v.currentTime = ratio * duration
  }

  const changeVolume = (val) => {
    const v = videoRef.current
    if (!v) return
    v.volume = val
    v.muted = val === 0
    setVolume(val)
    setMuted(val === 0)
  }

  const toggleMute = () => {
    const v = videoRef.current
    if (!v) return
    v.muted = !v.muted
    setMuted(v.muted)
  }

  const toggleFullscreen = () => {
    if (!containerRef.current) return
    if (document.fullscreenElement) document.exitFullscreen()
    else containerRef.current.requestFullscreen()
  }

  const resetHideTimer = () => {
    setShowControls(true)
    clearTimeout(hideTimer.current)
    hideTimer.current = setTimeout(() => setShowControls(false), 2800)
  }

  useEffect(() => () => clearTimeout(hideTimer.current), [])

  // Fullscreen-specific fallback for showing the controls again.
  //
  // React's onMouseMove on .vp-container (used everywhere else) stops
  // reliably reaching this component once the element is the fullscreen
  // element on some browsers: after the OS/browser auto-hides the system
  // cursor over a fullscreen <video> from inactivity, the next real mouse
  // move first has to reveal that cursor again, and that first move can be
  // consumed by the browser's own idle-cursor handling before it ever
  // reaches page JS - so once the panel auto-hides, moving the mouse looks
  // like it does nothing. A document-level, capture-phase listener bypasses
  // that: it does not depend on the cursor already being visible or on the
  // move landing exactly inside React's synthetic-event tree.
  useEffect(() => {
    // Holds the current mousemove/mousedown pair's own removal function, so
    // the fullscreenchange handler below can tear down exactly the listeners
    // it added - not a DOM node property, just a ref cell for that closure.
    let removeMoveListeners = null

    const onFullscreenChange = () => {
      const inFullscreen = document.fullscreenElement === containerRef.current
      removeMoveListeners?.()
      removeMoveListeners = null
      if (!inFullscreen) return

      resetHideTimer()
      const onMove = () => resetHideTimer()
      document.addEventListener('mousemove', onMove, true)
      document.addEventListener('mousedown', onMove, true)
      removeMoveListeners = () => {
        document.removeEventListener('mousemove', onMove, true)
        document.removeEventListener('mousedown', onMove, true)
      }
    }

    document.addEventListener('fullscreenchange', onFullscreenChange)
    return () => {
      document.removeEventListener('fullscreenchange', onFullscreenChange)
      removeMoveListeners?.()
    }
  }, [])

  // Player keyboard shortcuts (space/K play-pause, arrows seek/volume, M
  // mute, F fullscreen) - listened on the container rather than window, so
  // typing in the navbar's search box is never hijacked. The container needs
  // tabIndex to be a valid keydown target at all, and gets focused on mount
  // and on click so keys work immediately without the user having to tab to
  // it first (a plain <div> is not focusable/keyboard-reachable by default).
  useEffect(() => {
    const el = containerRef.current
    if (!el) return

    const onKeyDown = (e) => {
      // Let the browser handle typing/navigation inside an open menu popover.
      if (e.target !== el && e.target.tagName !== 'VIDEO') return
      const v = videoRef.current
      if (!v) return

      switch (e.key) {
        case ' ':
        case 'k':
        case 'K':
          e.preventDefault()
          togglePlay()
          break
        case 'ArrowRight':
          e.preventDefault()
          skip(10)
          break
        case 'ArrowLeft':
          e.preventDefault()
          skip(-10)
          break
        case 'ArrowUp':
          e.preventDefault()
          changeVolume(Math.min(1, (v.muted ? 0 : v.volume) + 0.1))
          break
        case 'ArrowDown':
          e.preventDefault()
          changeVolume(Math.max(0, (v.muted ? 0 : v.volume) - 0.1))
          break
        case 'm':
        case 'M':
          toggleMute()
          break
        case 'f':
        case 'F':
          toggleFullscreen()
          break
        default:
          return
      }
      resetHideTimer()
    }

    el.addEventListener('keydown', onKeyDown)
    // Focus on mount so shortcuts work as soon as the page opens, not only
    // after the user clicks the player once.
    el.focus()
    return () => el.removeEventListener('keydown', onKeyDown)
  }, [duration])

  const progressPct = duration ? (progress / duration) * 100 : 0

  return (
    <div
      ref={containerRef}
      className="vp-container"
      // -1: focusable via .focus() for keyboard shortcuts, but not part of
      // the page's Tab order (it's a video surface, not a form control).
      tabIndex={-1}
      onMouseMove={resetHideTimer}
      onClick={(e) => {
        containerRef.current?.focus()
        if (e.target === e.currentTarget) togglePlay()
      }}
    >
      {!started && posterUrl && (
        <img className="vp-poster" src={proxiedImageUrl(posterUrl)} alt="" />
      )}

      {/* Lives inside .vp-container (not the page) so it still renders once
          fullscreen promotes this element - anything outside it is invisible
          in fullscreen, since the Fullscreen API only shows one element's
          own subtree. */}
      {title && (
        <div className={`vp-title-bar ${showControls ? 'is-visible' : ''}`}>
          <div className="vp-title-main">{title}</div>
          {subtitle && <div className="vp-title-sub">{subtitle}</div>}
        </div>
      )}

      <video
        ref={videoRef}
        className="vp-video"
        onClick={togglePlay}
        // Required for cross-origin <track> cue files (the subtitle CDN
        // sends Access-Control-Allow-Origin: *, but without this attribute
        // the browser still refuses to load/parse the cues at all - the
        // menu populates fine either way since that's just JS state, but
        // the actual subtitle text never renders without crossOrigin set.
        crossOrigin="anonymous"
      >
        {subtitleTracks.map((t) => (
          // No `default` prop here on purpose: mixing it with imperative
          // mode-setting (see setSubtitle in usePlayer.js) causes the track
          // to reset/re-mount and lose its "showing" state every time
          // currentSubtitle changes and this element re-renders. Visibility
          // is controlled entirely via textTrack.mode instead.
          <track key={t.url} kind="subtitles" src={t.url} srcLang={t.lang.slice(0, 2).toLowerCase()} label={t.lang} />
        ))}
      </video>

      {(buffering || !started) && (
        <div className="vp-spinner-overlay">
          <div className="vp-spinner" />
        </div>
      )}

      <div className={`vp-controls ${showControls ? 'is-visible' : ''}`}>
        <div className="vp-seek-row">
          <div
            className="vp-seek-bar"
            onClick={(e) => {
              const rect = e.currentTarget.getBoundingClientRect()
              seekTo((e.clientX - rect.left) / rect.width)
            }}
          >
            <div className="vp-seek-fill" style={{ width: `${progressPct}%` }} />
            <div className="vp-seek-knob" style={{ left: `${progressPct}%` }} />
          </div>
        </div>

        <div className="vp-btn-row">
          <div className="vp-btn-row-left">
            <button type="button" className="vp-btn" onClick={togglePlay}>
              {playing ? <PauseIcon /> : <PlayIcon />}
            </button>
            <button type="button" className="vp-btn" onClick={() => skip(-10)} title="Back 10s">
              <BackIcon />
            </button>
            <button type="button" className="vp-btn" onClick={() => skip(10)} title="Forward 10s">
              <FwdIcon />
            </button>
            <button type="button" className="vp-btn" onClick={toggleMute}>
              {muted || volume === 0 ? <MuteIcon /> : <VolumeIcon />}
            </button>
            <input
              type="range"
              className="vp-volume"
              min="0"
              max="1"
              step="0.05"
              value={muted ? 0 : volume}
              onChange={(e) => changeVolume(Number(e.target.value))}
            />
            <span className="vp-time">
              {formatTime(progress)} / {formatTime(duration)}
            </span>
          </div>

          <div className="vp-btn-row-right">
            {boostSupported && (
              <div className="vp-menu-wrap">
                <button
                  type="button"
                  className={`vp-btn${boost > 1 ? ' vp-btn-active' : ''}`}
                  onClick={() => setMenu(menu === 'audio' ? null : 'audio')}
                  title="Audio boost"
                >
                  <BoostIcon />
                </button>
                {menu === 'audio' && (
                  <div className="vp-menu vp-menu-audio">
                    <div className="vp-menu-title">
                      Audio boost
                      <span className="vp-boost-value">{boost.toFixed(1)}x</span>
                    </div>
                    <input
                      type="range"
                      className="vp-boost-range"
                      min="1"
                      max={MAX_BOOST}
                      step="0.1"
                      value={boost}
                      onChange={(e) => setBoost(Number(e.target.value))}
                    />
                    <div className="vp-menu-hint">
                      For titles mastered quieter than normal. Near the top of
                      the range, loud scenes may distort.
                    </div>
                  </div>
                )}
              </div>
            )}
            {subtitleTracks.length > 0 && (
              <div className="vp-menu-wrap">
                <button
                  type="button"
                  className="vp-btn"
                  onClick={() => setMenu(menu === 'subtitles' ? null : 'subtitles')}
                  title="Subtitles"
                >
                  <SubtitleIcon />
                </button>
                {menu === 'subtitles' && (
                  <div className="vp-menu">
                    <button
                      type="button"
                      className={currentSubtitle === -1 ? 'is-active' : ''}
                      onClick={() => {
                        setSubtitle(-1)
                        setMenu(null)
                      }}
                    >
                      Off
                    </button>
                    {subtitleTracks.map((t, i) => (
                      <button
                        type="button"
                        key={t.url}
                        className={currentSubtitle === i ? 'is-active' : ''}
                        onClick={() => {
                          setSubtitle(i)
                          setMenu(null)
                        }}
                      >
                        {t.lang}
                      </button>
                    ))}
                    {currentSubtitle !== -1 && (
                      // Subtitle files come from a third-party CDN rather
                      // than being authored against this exact stream (see
                      // usePlayer.js's subtitleOffset docstring), so a
                      // fixed timing mismatch is common - this lets it be
                      // corrected per-title rather than living with it.
                      <div className="vp-subtitle-sync">
                        <span className="vp-subtitle-sync-label">
                          Sync: {subtitleOffset > 0 ? '+' : ''}
                          {subtitleOffset.toFixed(1)}s
                        </span>
                        <div className="vp-subtitle-sync-buttons">
                          <button
                            type="button"
                            onClick={() => setSubtitleOffset((o) => Math.round((o - 0.5) * 10) / 10)}
                            title="Subtitles appearing too early? Delay them."
                          >
                            −0.5s
                          </button>
                          <button
                            type="button"
                            onClick={() => setSubtitleOffset(0)}
                            title="Reset sync"
                          >
                            Reset
                          </button>
                          <button
                            type="button"
                            onClick={() => setSubtitleOffset((o) => Math.round((o + 0.5) * 10) / 10)}
                            title="Subtitles appearing too late? Advance them."
                          >
                            +0.5s
                          </button>
                        </div>
                      </div>
                    )}
                  </div>
                )}
              </div>
            )}

            {levels.length > 1 && (
              <div className="vp-menu-wrap">
                <button
                  type="button"
                  className="vp-btn"
                  onClick={() => setMenu(menu === 'quality' ? null : 'quality')}
                  title="Quality"
                >
                  <GearIcon />
                </button>
                {menu === 'quality' && (
                  <div className="vp-menu">
                    <button
                      type="button"
                      className={currentLevel === -1 ? 'is-active' : ''}
                      onClick={() => {
                        setQuality(-1)
                        setMenu(null)
                      }}
                    >
                      Auto
                    </button>
                    {levels
                      .slice()
                      .sort((a, b) => b.height - a.height)
                      .map((lvl) => {
                        const size = estimateSize(lvl.bitrate, measuredBitrates[lvl.index], duration)
                        return (
                          <button
                            type="button"
                            key={lvl.index}
                            className={currentLevel === lvl.index ? 'is-active' : ''}
                            onClick={() => {
                              setQuality(lvl.index)
                              setMenu(null)
                            }}
                          >
                            <span className="vp-quality-label">{lvl.height}p</span>
                            {size && (
                              <span
                                className="vp-quality-size"
                                title={size.measured ? 'Measured from this playback' : 'Estimated from stream bandwidth'}
                              >
                                {size.label}
                              </span>
                            )}
                          </button>
                        )
                      })}
                  </div>
                )}
              </div>
            )}

            <button type="button" className="vp-btn" onClick={toggleFullscreen} title="Fullscreen">
              <FullscreenIcon />
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}

function PlayIcon() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor">
      <path d="M8 5v14l11-7z" />
    </svg>
  )
}
function PauseIcon() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor">
      <path d="M6 5h4v14H6zM14 5h4v14h-4z" />
    </svg>
  )
}
function BackIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor">
      <path d="M11 5V1L5 7l6 6V9c3.31 0 6 2.69 6 6s-2.69 6-6 6-6-2.69-6-6H3c0 4.42 3.58 8 8 8s8-3.58 8-8-3.58-8-8-8z" />
    </svg>
  )
}
function FwdIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor">
      <path d="M13 5V1l6 6-6 6V9c-3.31 0-6 2.69-6 6s2.69 6 6 6 6-2.69 6-6h2c0 4.42-3.58 8-8 8s-8-3.58-8-8 3.58-8 8-8z" />
    </svg>
  )
}
function BoostIcon() {
  // Speaker with a "+" - the volume icon's sibling, marking added gain
  // rather than the attenuation the plain volume control does.
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor">
      <path d="M3 10v4h4l5 5V5L7 10H3z" />
      <path d="M19 8h-2v3h-3v2h3v3h2v-3h3v-2h-3V8z" />
    </svg>
  )
}
function VolumeIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor">
      <path d="M3 10v4h4l5 5V5L7 10H3zm13.5 2A4.5 4.5 0 0 0 14 7.97v8.05A4.5 4.5 0 0 0 16.5 12z" />
    </svg>
  )
}
function MuteIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor">
      <path d="M16.5 12A4.5 4.5 0 0 0 14 7.97v2.21l2.45 2.45c.03-.2.05-.42.05-.63zM19 12c0 .94-.2 1.82-.54 2.64l1.51 1.51C20.63 14.91 21 13.5 21 12c0-4.28-2.99-7.86-7-8.77v2.06c2.89.86 5 3.54 5 6.71zM4.27 3 3 4.27 7.73 9H3v6h4l5 5v-6.73l4.25 4.25c-.67.52-1.42.93-2.25 1.18v2.06a8.99 8.99 0 0 0 3.69-1.81L19.73 21 21 19.73l-9-9L4.27 3zM12 4 9.91 6.09 12 8.18V4z" />
    </svg>
  )
}
function SubtitleIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor">
      <path d="M20 4H4c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V6c0-1.1-.9-2-2-2zm-8 8H6v-2h6v2zm6 0h-4v-2h4v2zM8 8H6V6h2v2z" />
    </svg>
  )
}
function GearIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor">
      <path d="M19.14 12.94c.04-.3.06-.61.06-.94s-.02-.64-.07-.94l2.03-1.58a.5.5 0 0 0 .12-.61l-1.92-3.32a.5.5 0 0 0-.58-.22l-2.39.96a7.4 7.4 0 0 0-1.62-.94l-.36-2.54a.5.5 0 0 0-.5-.42h-3.84a.5.5 0 0 0-.5.42l-.36 2.54c-.59.24-1.13.56-1.62.94l-2.39-.96a.5.5 0 0 0-.58.22L2.7 8.87a.5.5 0 0 0 .12.61l2.03 1.58a7.3 7.3 0 0 0 0 1.88l-2.03 1.58a.5.5 0 0 0-.12.61l1.92 3.32c.13.22.39.31.58.22l2.39-.96c.5.38 1.03.7 1.62.94l.36 2.54c.05.24.26.42.5.42h3.84c.24 0 .45-.18.5-.42l.36-2.54a7.4 7.4 0 0 0 1.62-.94l2.39.96c.22.09.48 0 .58-.22l1.92-3.32a.5.5 0 0 0-.12-.61l-2.03-1.58zM12 15.5a3.5 3.5 0 1 1 0-7 3.5 3.5 0 0 1 0 7z" />
    </svg>
  )
}
function FullscreenIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor">
      <path d="M7 14H5v5h5v-2H7v-3zm-2-4h2V7h3V5H5v5zm12 7h-3v2h5v-5h-2v3zM14 5v2h3v3h2V5h-5z" />
    </svg>
  )
}
