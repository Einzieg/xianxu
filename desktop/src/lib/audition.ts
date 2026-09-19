export const AUDITION_STOP_EVENT = 'xianxu:stop-audition';

// Module scope keeps the latch alive across track changes and component remounts.
let inFlight = false;

export async function runAuditionRequest<T>(request: () => Promise<T>): Promise<
  { status: 'busy' } | { status: 'done'; value: T }
> {
  if (inFlight) return { status: 'busy' };
  inFlight = true;
  try {
    return { status: 'done', value: await request() };
  } finally {
    // Only the owning request may release the latch, never a UI clear/unmount.
    inFlight = false;
  }
}
