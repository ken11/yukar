/**
 * Short notification chime using the Web Audio API.
 * - success: ascending 2-tone (440Hz → 660Hz)
 * - error: low single tone (220Hz)
 *
 * Failures due to browser autoplay restrictions are caught silently with try/catch.
 * No audio files are added.
 */

let sharedCtx: AudioContext | null = null;

function getAudioContext(): AudioContext | null {
  if (typeof window === "undefined") return null;
  try {
    if (!sharedCtx) {
      sharedCtx = new AudioContext();
    }
    return sharedCtx;
  } catch {
    return null;
  }
}

function playTone(
  ctx: AudioContext,
  frequency: number,
  startTime: number,
  duration: number,
  gain = 0.15,
): void {
  const osc = ctx.createOscillator();
  const gainNode = ctx.createGain();

  osc.connect(gainNode);
  gainNode.connect(ctx.destination);

  osc.type = "sine";
  osc.frequency.setValueAtTime(frequency, startTime);

  gainNode.gain.setValueAtTime(gain, startTime);
  gainNode.gain.exponentialRampToValueAtTime(0.001, startTime + duration);

  osc.start(startTime);
  osc.stop(startTime + duration);
}

export function playChime(kind: "success" | "error"): void {
  try {
    const ctx = getAudioContext();
    if (!ctx) return;

    // resume only succeeds after a user gesture. Failure is ignored.
    const doPlay = () => {
      const now = ctx.currentTime;
      if (kind === "success") {
        playTone(ctx, 440, now, 0.15);
        playTone(ctx, 660, now + 0.18, 0.2);
      } else {
        playTone(ctx, 220, now, 0.3, 0.12);
      }
    };

    if (ctx.state === "suspended") {
      ctx
        .resume()
        .then(doPlay)
        .catch(() => {});
    } else {
      doPlay();
    }
  } catch {
    // Don't break the UI even if autoplay restrictions or other issues cause failure
  }
}
