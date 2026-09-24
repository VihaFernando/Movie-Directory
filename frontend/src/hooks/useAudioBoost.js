import { useCallback, useEffect, useRef, useState } from 'react'

// Maximum multiplier the boost slider allows.
//
// Capped at 2.5x deliberately, not by feel: measured against a real quiet
// title (-24.6 LUFS integrated, -8.3 dBFS true peak), 2.5x lands at
// -16.6 LUFS with peaks at -0.3 dBFS - essentially the streaming norm with
// the headroom just used up. 3x on the same source pushes peaks to
// +1.2 dBFS, which clips and audibly distorts loud scenes, so the ceiling
// sits below it.
export const MAX_BOOST = 2.5

// Amplifies a <video> element's audio beyond the 100% that element.volume
// caps at, by routing it through a Web Audio GainNode.
//
// Why this exists: some sources are mastered far quieter than the streaming
// norm, and element.volume is a pure attenuator - it cannot exceed 1.0, so
// at full volume the browser is already doing everything it can. A GainNode
// is the only way to actually add level.
//
// The graph (createMediaElementSource -> gain -> destination) is built lazily
// on the first boost above 1x rather than on mount, for two reasons: an
// AudioContext starts suspended until a user gesture, and - more importantly -
// createMediaElementSource() permanently reroutes the element's audio through
// the graph. Building it only when needed keeps normal-loudness playback on
// the browser's own untouched audio path.
export function useAudioBoost(videoRef) {
  const [boost, setBoostState] = useState(1)
  const [supported, setSupported] = useState(true)
  const ctxRef = useRef(null)
  const gainRef = useRef(null)
  const sourceRef = useRef(null)

  const ensureGraph = useCallback(() => {
    if (gainRef.current) return true
    const video = videoRef.current
    if (!video) return false

    const Ctx = window.AudioContext || window.webkitAudioContext
    if (!Ctx) {
      setSupported(false)
      return false
    }

    try {
      const ctx = new Ctx()
      // createMediaElementSource() throws if the element is cross-origin
      // without CORS. Our media is same-origin (everything is served through
      // /api/proxy_embed), so this succeeds - but a throw here must not take
      // playback down with it, hence the catch below.
      const source = ctx.createMediaElementSource(video)
      const gain = ctx.createGain()
      gain.gain.value = 1
      source.connect(gain)
      gain.connect(ctx.destination)

      ctxRef.current = ctx
      gainRef.current = gain
      sourceRef.current = source
      return true
    } catch (err) {
      console.warn('Audio boost unavailable:', err)
      setSupported(false)
      return false
    }
  }, [videoRef])

  const setBoost = useCallback(
    (value) => {
      const clamped = Math.min(Math.max(value, 1), MAX_BOOST)

      // 1x is the neutral setting, so don't build the graph just to apply it -
      // that would reroute audio through Web Audio for no gain (literally).
      if (clamped === 1 && !gainRef.current) {
        setBoostState(1)
        return
      }
      if (!ensureGraph()) return

      // An AudioContext created before a user gesture starts suspended;
      // resuming here is safe because this only ever runs from a slider input.
      if (ctxRef.current?.state === 'suspended') ctxRef.current.resume().catch(() => {})

      gainRef.current.gain.value = clamped
      setBoostState(clamped)
    },
    [ensureGraph],
  )

  // Tear the graph down with the component. The AudioContext holds real audio
  // hardware resources, and browsers cap how many can exist at once, so
  // leaking one per opened title would eventually break playback outright.
  useEffect(() => {
    return () => {
      try {
        sourceRef.current?.disconnect()
        gainRef.current?.disconnect()
        ctxRef.current?.close()
      } catch {
        // Already closed/disconnected - nothing to clean up.
      }
      ctxRef.current = null
      gainRef.current = null
      sourceRef.current = null
    }
  }, [])

  return { boost, setBoost, boostSupported: supported }
}
