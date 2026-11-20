import { useId } from "react";

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
