// Optional pure-JavaScript regression: node tests/test_webui_duration.mjs
import assert from 'node:assert/strict';
import {parseSeconds, durationSeconds, convertDuration} from '../webui/static/duration.mjs';

for (const [value, unit, expected] of [['60','seconds','60'], ['2','minutes','120'], ['0.55','minutes','33'], ['0.1','minutes','6'], ['1.000','seconds','1'], ['1440','minutes','86400']]) {
  assert.equal(parseSeconds(value, unit), expected);
}
for (const value of ['', '0', '-1', '1.5', 'NaN', 'Infinity', '86401', '1e2', '1.0000000000000001']) {
  assert.throws(() => parseSeconds(value, 'seconds'));
}
for (const value of ['0.01', '0.016666666666666666', '1440.01']) assert.throws(() => parseSeconds(value, 'minutes'));
for (let value = 1; value <= 86400; value++) {
  const seconds = String(value);
  const minutes = convertDuration({value:seconds, unit:'seconds'}, 'minutes');
  assert.equal(durationSeconds(minutes), seconds);
  assert.equal(convertDuration(minutes, 'seconds').value, seconds);
}
const edited = {...convertDuration({value:'31',unit:'seconds'}, 'minutes'), value:'0.55'};
delete edited.exactSeconds; // Input edits invalidate the exact unit-conversion cache.
assert.equal(durationSeconds(edited), '33');
console.log('Duration regression passed: exact decimals, invalid inputs, unit round trips, input edits.');
