import { useId, type ReactNode } from "react";

type GearProps = { size?: number; teeth?: number; spin?: boolean; className?: string; title?: string };

/** A machined gear drawn from rotated teeth and a punched-out hub, so it inherits `currentColor`. */
export function Gear({ size = 20, teeth = 8, spin = false, className = "", title }: GearProps) {
  const mask = useId();
  const angles = Array.from({ length: teeth }, (_, i) => (360 / teeth) * i);
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" className={`gear ${spin ? "spin" : ""} ${className}`}
      role={title ? "img" : undefined} aria-hidden={title ? undefined : true} aria-label={title}>
      <mask id={mask}>
        <rect width="24" height="24" fill="white" />
        <circle cx="12" cy="12" r="3.2" fill="black" />
      </mask>
      <g fill="currentColor" mask={`url(#${mask})`}>
        <circle cx="12" cy="12" r="7.4" />
        {angles.map((a) => (
          <rect key={a} x="10.2" y="1.2" width="3.6" height="5" rx="0.8" transform={`rotate(${a} 12 12)`} />
        ))}
      </g>
    </svg>
  );
}

/** Two meshed gears: the Software Factory mark. */
export function Logo({ size = 34 }: { size?: number }) {
  return (
    <span className="logo" style={{ width: size * 1.35, height: size }} aria-hidden>
      <Gear size={size} teeth={10} className="logo-big" />
      <Gear size={size * 0.62} teeth={8} className="logo-small" />
    </span>
  );
}

type IconProps = { size?: number };

function Stroke({ size = 18, children }: IconProps & { children: ReactNode }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
      strokeLinecap="round" strokeLinejoin="round" aria-hidden className="icon">
      {children}
    </svg>
  );
}

export function Camera(props: IconProps) {
  return (
    <Stroke {...props}>
      <rect x="2.5" y="6" width="13" height="12" rx="2" />
      <path d="M15.5 10.5 21.5 7v10l-6-3.5z" />
    </Stroke>
  );
}

export function Mic(props: IconProps) {
  return (
    <Stroke {...props}>
      <rect x="9" y="2.5" width="6" height="12" rx="3" />
      <path d="M5 11a7 7 0 0 0 14 0M12 18v3.5" />
    </Stroke>
  );
}

export function MicOff(props: IconProps) {
  return (
    <Stroke {...props}>
      <path d="M15 9.5V5.5a3 3 0 0 0-5.7-1.3M9 9v2.5a3 3 0 0 0 4.6 2.5M5 11a7 7 0 0 0 11.2 5.6M19 11a7 7 0 0 1-.6 2.8M12 18v3.5M3 3l18 18" />
    </Stroke>
  );
}

export function HangUp(props: IconProps) {
  return (
    <Stroke {...props}>
      <path d="M3 14.5c5-4.7 13-4.7 18 0l-2.2 2.6-3.6-1.5.2-2.6c-2.3-.8-4.5-.8-6.8 0l.2 2.6-3.6 1.5z" />
    </Stroke>
  );
}
