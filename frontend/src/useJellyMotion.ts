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
  const press = useMotionValue(0)
  const springOptions = { stiffness: 280, damping: 22, mass: 0.65 }
  const springX = useSpring(pointerX, springOptions)
  const springY = useSpring(pointerY, springOptions)
  const springPress = useSpring(press, { stiffness: 420, damping: 26, mass: 0.5 })
  const translateX = useTransform(springX, [-1, 1], [-5, 5])
  const translateY = useTransform(springY, [-1, 1], [-5, 5])
  const rotateX = useTransform(springY, [-1, 1], [3.5, -3.5])
  const rotateY = useTransform(springX, [-1, 1], [-4.5, 4.5])
  const scale = useTransform(springPress, [0, 1], [1, 0.975])
  const highlightX = useTransform(springX, [-1, 1], ['18%', '82%'])
  const highlightY = useTransform(springY, [-1, 1], ['10%', '38%'])
  const active = enabled && !reducedMotion

  useEffect(() => {
    if (active) return
    pointerX.jump(0)
    pointerY.jump(0)
    press.jump(0)
  }, [active, pointerX, pointerY, press])

  const reset = () => {
    pointerX.set(0)
    pointerY.set(0)
    press.set(0)
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
    transformPerspective: 900,
    '--jelly-highlight-x': highlightX,
    '--jelly-highlight-y': highlightY,
  } as MotionStyle : undefined

  return {
    style,
    onPointerMove,
    onPointerDown,
    onPointerUp,
    onPointerLeave: reset,
    onPointerCancel: reset,
  }
}
