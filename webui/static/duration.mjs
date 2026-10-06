// Decimal arithmetic avoids rounding valid minute inputs (for example 0.55).
export function parseSeconds(value, unit) {
  const text = value.trim();
  if (text.length > 32 || !/^[0-9]+(?:\.[0-9]+)?$/.test(text)) throw new Error('禁言时长不能为空，且必须是正数');
  if (!['seconds', 'minutes'].includes(unit)) throw new Error('无效的时长单位');
  const [whole, fraction = ''] = text.split('.');
  const scale = 10n ** BigInt(fraction.length);
  const numerator = BigInt(whole + fraction) * (unit === 'minutes' ? 60n : 1n);
  if (numerator % scale !== 0n || numerator < scale || numerator > 86400n * scale) throw new Error('换算后必须为 1～86400 的整数秒');
  return String(numerator / scale);
}

export function durationSeconds(duration) {
  // Keep the exact seconds when the displayed minutes have a repeating decimal.
  // Editing the input drops this cache, so user-entered fractions stay strict.
  return duration.exactSeconds === undefined ? parseSeconds(duration.value, duration.unit) : parseSeconds(duration.exactSeconds, 'seconds');
}

export function convertDuration(duration, unit) {
  const seconds = durationSeconds(duration);
  if (!['seconds', 'minutes'].includes(unit)) throw new Error('无效的时长单位');
  return {value: unit === 'minutes' ? String(Number(seconds) / 60) : seconds, unit, exactSeconds: seconds};
}
