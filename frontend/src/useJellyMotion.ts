import { useEffect, useState, type PointerEvent as ReactPointerEvent } from 'react'
import { useMotionValue, useSpring, useTransform, type MotionStyle } from 'motion/react'

const REDUCED_MOTION_QUERY = '(prefers-reduced-motion: reduce)'

function usePrefersReducedMotion() {
  const [reducedMotion, setReducedMotion] = useState(() => (
    typeof window !== 'undefined' && typeof window.matchMedia === 'function'
      ? window.matchMedia(REDUCED_MOTION_QUERY).matches
      : false
  ))

  useEffect(() => {
    if (typeof window.matchMedia !== 'function') return
    const media = window.matchMedia(REDUCED_MOTION_QUERY)
    const update = () => setReducedMotion(media.matches)
    update()
    media.addEventListener?.('change', update)
    return () => media.removeEventListener?.('change', update)
  }, [])

  return reducedMotion
}

export function useJellyMotion(enabled: boolean) {
  const reducedMotion = usePrefersReducedMotion()
  const pointerX = useMotionValue(0)
  const pointerY = useMotionValue(0)
  const hover = useMotionValue(0)
  const press = useMotionValue(0)
  const springOptions = { stiffness: 280, damping: 22, mass: 0.65 }
  const springX = useSpring(pointerX, springOptions)
  const springY = useSpring(pointerY, springOptions)
  const springHover = useSpring(hover, { stiffness: 300, damping: 20, mass: 0.55 })
  const springPress = useSpring(press, { stiffness: 420, damping: 26, mass: 0.5 })
  const translateX = useTransform(() => springX.get() * 5)
  const translateY = useTransform(() => springY.get() * 5 + springHover.get() * -4)
  const rotateX = useTransform(() => springY.get() * -3.5)
  const rotateY = useTransform(() => springX.get() * 4.5)
  const scale = useTransform(() => 1 + springHover.get() * 0.012 - springPress.get() * 0.02)
  const scaleX = useTransform(() => 1 + springHover.get() * 0.008 + springX.get() * 0.008 - springPress.get() * 0.012)
  const scaleY = useTransform(() => 1 + springHover.get() * 0.008 - springY.get() * 0.006 - springPress.get() * 0.026)
  const highlightX = useTransform(() => `${50 + springX.get() * 32}%`)
  const highlightY = useTransform(() => `${24 + springY.get() * 14}%`)
  const active = enabled && !reducedMotion

  useEffect(() => {
    if (active) return
    pointerX.jump(0)
    pointerY.jump(0)
    hover.jump(0)
    press.jump(0)
    springX.jump(0)
    springY.jump(0)
    springHover.jump(0)
    springPress.jump(0)
  }, [active, hover, pointerX, pointerY, press, springHover, springPress, springX, springY])

  const reset = () => {
    pointerX.set(0)
    pointerY.set(0)
    hover.set(0)
    press.set(0)
  }

  const onPointerEnter = (event: ReactPointerEvent<HTMLElement>) => {
    if (active && event.pointerType === 'mouse') hover.set(1)
  }

  const onPointerMove = (event: ReactPointerEvent<HTMLElement>) => {
    if (!active || event.pointerType !== 'mouse') return
    const rect = event.currentTarget.getBoundingClientRect()
    if (!rect.width || !rect.height) return
    pointerX.set(Math.max(-1, Math.min(1, ((event.clientX - rect.left) / rect.width) * 2 - 1)))
    pointerY.set(Math.max(-1, Math.min(1, ((event.clientY - rect.top) / rect.height) * 2 - 1)))
  }

  const onPointerDown = (event: ReactPointerEvent<HTMLElement>) => {
    if (active && event.pointerType === 'mouse') press.set(1)
  }

  const onPointerUp = (event: ReactPointerEvent<HTMLElement>) => {
    if (event.pointerType === 'mouse') press.set(0)
  }

  const style = active ? {
    x: translateX,
    y: translateY,
    rotateX,
    rotateY,
    scale,
    scaleX,
    scaleY,
    transformPerspective: 900,
    '--jelly-highlight-x': highlightX,
    '--jelly-highlight-y': highlightY,
  } as MotionStyle : {
    transform: 'none',
    '--jelly-highlight-x': '50%',
    '--jelly-highlight-y': '24%',
  } as MotionStyle

  return {
    style,
    onPointerEnter,
    onPointerMove,
    onPointerDown,
    onPointerUp,
    onPointerLeave: reset,
    onPointerCancel: reset,
  }
}
