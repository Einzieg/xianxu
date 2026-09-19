import type { CSSProperties } from 'react';

const paths = {
  library: <><rect x="3" y="3" width="7" height="18" rx="2" /><path d="m14 4 5-1 3 17-5 1zM6.5 7v7" /></>,
  audio: <><path d="M4 10v4m4-8v12m4-16v20m4-16v12m4-8v4" /></>,
  settings: <><path d="m9 3-.6 2.4-2 1.2L4 6l-2 3 1.8 1.8v2.4L2 15l2 3 2.4-.6 2 1.2L9 21h6l.6-2.4 2-1.2 2.4.6 2-3-1.8-1.8v-2.4L22 9l-2-3-2.4.6-2-1.2L15 3z" /><circle cx="12" cy="12" r="3" /></>,
  plus: <path d="M12 5v14M5 12h14" />,
  import: <><path d="M12 3v12m-4-4 4 4 4-4M4 16v4h16v-4" /></>,
  export: <><path d="M12 16V3m-4 4 4-4 4 4M4 16v4h16v-4" /></>,
  search: <><circle cx="10.5" cy="10.5" r="6.5" /><path d="m16 16 5 5" /></>,
  music: <><path d="M9 18V5l11-2v13M9 9l11-2" /><ellipse cx="6" cy="18" rx="3" ry="3" /><ellipse cx="17" cy="16" rx="3" ry="3" /></>,
  play: <path d="m8 4 13 8-13 8z" />,
  pause: <><path d="M8 5v14M16 5v14" strokeWidth="4" /></>,
  stop: <rect x="6" y="6" width="12" height="12" rx="2" />,
  previous: <><path d="M5 5v14m14-14L8 12l11 7z" /></>,
  next: <><path d="M19 5v14M5 5l11 7-11 7z" /></>,
  repeat: <><path d="m17 2 4 4-4 4M3 10V6h18M7 22l-4-4 4-4m14 0v4H3" /></>,
  shuffle: <><path d="M3 5h3l12 14h3M3 19h3L18 5h3m-4-3 4 3-4 3m0 8 4 3-4 3" /></>,
  order: <><path d="M4 6h15M4 12h11M4 18h7m6-5 4 4-4 4m4-4h-7" /></>,
  target: <><circle cx="12" cy="12" r="7" /><circle cx="12" cy="12" r="2" /><path d="M12 1v4m0 14v4M1 12h4m14 0h4" /></>,
  shield: <><path d="m12 2 8 4v6c0 5-8 10-8 10S4 17 4 12V6z" /><path d="m8 12 3 3 5-6" /></>,
  check: <path d="m5 12 4 4L19 6" />,
  close: <path d="m6 6 12 12M6 18 18 6" />,
  trash: <><path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7" /></>,
  folder: <path d="M3 7V4h7l2 3h9v13H3z" />,
  arrow: <path d="M4 12h16m-6-6 6 6-6 6" />,
  info: <><circle cx="12" cy="12" r="9" /><path d="M12 11v6m0-10v.1" /></>,
  clock: <><circle cx="12" cy="12" r="9" /><path d="M12 6v6l4 2" /></>,
  text: <><path d="M4 4h16M12 4v16m-4 0h8M4 4v3m16-3v3" /></>,
  headphones: <><path d="M3 14v-3a9 9 0 0 1 18 0v3" /><rect x="2" y="12" width="5" height="9" rx="2" /><rect x="17" y="12" width="5" height="9" rx="2" /></>,
  expand: <path d="M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5M3 3l6 6m12-6-6 6M3 21l6-6m12 6-6-6" />,
  shrink: <><rect x="3" y="4" width="18" height="16" rx="2" /><rect x="5" y="6" width="7" height="5" rx="1" /></>,
} as const;

export type IconName = keyof typeof paths;
export default function Icon({ name, size = 20, style }: { name: IconName; size?: number; style?: CSSProperties }) {
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" style={style}>{paths[name]}</svg>;
}
