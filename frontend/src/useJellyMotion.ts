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
  const springOptions = { stiffness: 280, damping: 22, mass: 0.65 }
  const springX = useSpring(pointerX, springOptions)
  const springY = useSpring(pointerY, springOptions)
  const springHover = useSpring(hover, { stiffness: 300, damping: 20, mass: 0.55 })
  const translateX = useTransform(() => springX.get())
  const translateY = useTransform(() => springY.get() - springHover.get())
  const rotateX = useTransform(() => springY.get() * -2)
  const rotateY = useTransform(() => springX.get() * 2)
  const highlightX = useTransform(() => `${50 + springX.get() * 32}%`)
  const highlightY = useTransform(() => `${24 + springY.get() * 14}%`)
  const active = enabled && !reducedMotion

  useEffect(() => {
    if (active) return
    pointerX.jump(0)
    pointerY.jump(0)
    hover.jump(0)
    springX.jump(0)
    springY.jump(0)
    springHover.jump(0)
  }, [active, hover, pointerX, pointerY, springHover, springX, springY])

  const reset = () => {
    pointerX.set(0)
    pointerY.set(0)
    hover.set(0)
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

  const style = active ? {
    x: translateX,
    y: translateY,
    rotateX,
    rotateY,
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
    onPointerLeave: reset,
    onPointerCancel: reset,
  }
}
