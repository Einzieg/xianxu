import assert from 'node:assert/strict';
import test from 'node:test';
import { runAuditionRequest } from './audition.ts';
import { runTransport, transportAction } from './transport.ts';
import {
  DEFAULT_SETTINGS, EMPTY_STATE, MODES, fileName, filterLibrary, formatTime,
  getLocks, hasAudioSource, hasTarget, percent, pitchName, toDraft, transcriptionLabel, validateAuditionRange, validateSettings,
} from './model.ts';

test('timeline formatting clamps invalid values', () => {
  assert.equal(formatTime(125.9), '02:05');
  assert.equal(formatTime(-4), '00:00');
  assert.equal(formatTime(NaN), '00:00');
  assert.equal(percent(1.8), 100);
  assert.equal(percent(-1), 0);
  assert.equal(percent(NaN), 0);
});

test('nine-key settings accept the exact compiler limits', () => {
  for (const speed of ['0.25', '4']) {
    for (const hold of ['10', '150']) {
      assert.equal(validateSettings({ ...toDraft(DEFAULT_SETTINGS), speed, hold }).ok, true);
    }
  }
  for (const patch of [{ speed: '4.01' }, { speed: '0.24' }, { hold: '9' }, { hold: '151' }, { hold: '20.5' }, { delay: '31' }, { speed: '' }]) {
    assert.equal(validateSettings({ ...toDraft(DEFAULT_SETTINGS), ...patch }).ok, false);
  }
});

test('mapping validates length, unique pitches, unique keys and MIDI limits', () => {
  const draft = toDraft(DEFAULT_SETTINGS);
  assert.equal(validateSettings({ ...draft, mapping: draft.mapping.slice(1) }).ok, false);
  for (const entry of [{ pitch: '128', key: 'B' }, { pitch: '45', key: 'F8' }, { pitch: '', key: 'B' }, { pitch: '52', key: 'F' }]) {
    assert.equal(validateSettings({ ...draft, mapping: [entry, ...draft.mapping.slice(1)] }).ok, false);
  }
  const result = validateSettings({ ...draft, mapping: [{ pitch: '45', key: 'b' }, ...draft.mapping.slice(1)] });
  assert.equal(result.ok && result.value.mapping[0].key, 'B');
});

test('display defaults match the measured mapping and text score convention', () => {
  assert.deepEqual(DEFAULT_SETTINGS.mapping, [
    { pitch: 45, key: 'B' }, { pitch: 52, key: 'F' }, { pitch: 53, key: 'G' },
    { pitch: 55, key: 'H' }, { pitch: 57, key: 'J' }, { pitch: 59, key: 'K' },
    { pitch: 60, key: 'T' }, { pitch: 62, key: 'Y' }, { pitch: 64, key: 'U' },
  ]);
  assert.equal(DEFAULT_SETTINGS.octave, 3);
  assert.equal(DEFAULT_SETTINGS.hold, 60);
  assert.equal(DEFAULT_SETTINGS.bpm, 120);
  for (const patch of [{ octave: '3.5' }, { octave: '10' }, { bpm: '0' }, { bpm: '' }]) {
    assert.equal(validateSettings({ ...toDraft(DEFAULT_SETTINGS), ...patch }).ok, false);
  }
});

test('mutation locks cover paused, countdown, queue and asynchronous jobs', () => {
  assert.equal(getLocks(EMPTY_STATE, true, false).mutate, false);
  for (const state of ['playing', 'paused', 'countdown'] as const) {
    assert.equal(getLocks({ ...EMPTY_STATE, playback: { ...EMPTY_STATE.playback, state } }, true, false).mutate, true);
  }
  assert.equal(getLocks({ ...EMPTY_STATE, queue_active: true }, true, false).mutate, true);
  assert.equal(getLocks({ ...EMPTY_STATE, job: { id: '1', kind: 'capture', state: 'running', progress: 0, message: '' } }, true, false).start, true);
  assert.equal(getLocks(EMPTY_STATE, false, false).mutate, true);
  assert.equal(getLocks(EMPTY_STATE, true, true).mutate, true);
});

test('real playback needs a captured and non-invalid target', () => {
  assert.equal(hasTarget(EMPTY_STATE), false);
  assert.equal(hasTarget({ ...EMPTY_STATE, target: { title: 'Piano', state: 'ready' } }), true);
  assert.equal(hasTarget({ ...EMPTY_STATE, target: { title: 'Piano', state: 'lost' } }), false);
  assert.equal(hasTarget({ ...EMPTY_STATE, target: { title: 'Piano', state: 'background' } }), true);
});

test('transport uses explicit pause and resume, including countdown', async () => {
  for (const [state, expected] of [['paused', 'resume'], ['playing', 'pause'], ['countdown', 'pause'],
    ['stopped', 'play'], ['finished', 'play']] as const) {
    assert.equal(transportAction({ ...EMPTY_STATE, playback: { ...EMPTY_STATE.playback, state } }), expected);
  }
  const calls: unknown[] = [];
  await runTransport('pause', EMPTY_STATE, 'a', {
    prepareWindow: async () => { throw new Error('Pause must not resize/refocus'); },
    cancelled: () => false,
    send: async (method, params) => { calls.push([method, params]); },
  });
  assert.deepEqual(calls, [['pause', { paused: true }]]);
});

test('window shrink completes before play/resume asks backend to focus game', async () => {
  for (const action of ['play', 'resume'] as const) {
    const calls: unknown[] = [];
    await runTransport(action, EMPTY_STATE, 'a', {
      prepareWindow: async () => { calls.push('window'); },
      cancelled: () => false,
      send: async (method, params) => { calls.push([method, params]); },
    });
    assert.deepEqual(calls, ['window', [action, action === 'play'
      ? { activate_target: true, stop_sequence: 0, id: 'a', preview: false }
      : { activate_target: true, stop_sequence: 0 }]]);
  }
});

test('preview never requests focus, and stop during resize cancels pending play', async () => {
  let sent = false;
  await runTransport('play', EMPTY_STATE, 'a', {
    prepareWindow: async () => {}, cancelled: () => true,
    send: async () => { sent = true; },
  });
  assert.equal(sent, false);
  await runTransport('resume', { ...EMPTY_STATE, preview: true }, 'a', {
    prepareWindow: async () => {}, cancelled: () => false,
    send: async (_method, params) => { assert.deepEqual(params, { activate_target: false, stop_sequence: 0 }); },
  });
});

test('search supports names and sources without changing persisted order', () => {
  const items = [
    { id: '1', title: '春日', source: 'MIDI' },
    { id: '2', title: 'Nocturne', source: 'TXT' },
  ] as Parameters<typeof filterLibrary>[0];
  assert.deepEqual(filterLibrary(items, ' midi ').map((item) => item.id), ['1']);
  assert.deepEqual(filterLibrary(items, '春').map((item) => item.id), ['1']);
  assert.equal(filterLibrary(items, '').length, 2);
  assert.equal(items[0].id, '1');
});

test('all bridge mode values and pitch / path labels are preserved', () => {
  assert.deepEqual(MODES.map((mode) => mode.value), ['order', 'random', 'repeat_all', 'repeat_one']);
  assert.equal(pitchName(45), 'A2');
  assert.equal(fileName('C:\\music\\春日.wav'), '春日.wav');
  assert.equal(fileName('/music/track.mid'), 'track.mid');
});

test('audition range accepts defaults, fractional seconds and the 30-second limit', () => {
  assert.deepEqual(validateAuditionRange('0', '15'), { ok: true, value: { start: 0, duration: 15 } });
  assert.deepEqual(validateAuditionRange(' 12.5 ', '0.25'), { ok: true, value: { start: 12.5, duration: 0.25 } });
  assert.deepEqual(validateAuditionRange('0', '0.1'), { ok: true, value: { start: 0, duration: 0.1 } });
  assert.deepEqual(validateAuditionRange('120', '30'), { ok: true, value: { start: 120, duration: 30 } });
});

test('audition range rejects empty, negative and non-finite starts', () => {
  for (const start of ['', ' ', '-0.1', 'NaN', 'Infinity', '-Infinity', '1e309', 'abc']) {
    assert.equal(validateAuditionRange(start, '15').ok, false, start);
  }
});

test('audition range rejects invalid durations rather than silently clamping', () => {
  for (const duration of ['', ' ', '0', '-1', '0.01', '0.099', '30.01', 'NaN', 'Infinity', '1e309', 'abc']) {
    assert.equal(validateAuditionRange('0', duration).ok, false, duration);
  }
});

test('source audition only accepts supported audio file extensions', () => {
  for (const source of ['C:\\music\\春日.MP3', '/music/track.wav', 'solo.FlAc', 'theme.ogg']) {
    assert.equal(hasAudioSource(source), true, source);
  }
  for (const source of ['', 'TXT', 'MIDI', 'pasted text', 'track.txt', 'track.mid', 'track.MIDI', 'track.wav.txt', 'track.mp3.bak', 'C:\\music.wav\\track.mid', '/music.ogg/track', '/music.wav/']) {
    assert.equal(hasAudioSource(source), false, source);
  }
});

test('stop sequence starts at zero independently of the playback mode', () => {
  assert.equal(EMPTY_STATE.stop_sequence, 0);
  assert.equal(EMPTY_STATE.mode, 'order');
});

test('transcription labels distinguish saved legacy notes from the new decoder', () => {
  assert.equal(transcriptionLabel({ engine: 'instrument', texture: 'chords', revision: 'chords-v1' }), 'BASIC PITCH · 保留和弦 v1（实验）');
  assert.equal(transcriptionLabel({ engine: 'instrument', revision: 'legato-v2.1' }), 'BASIC PITCH · 重奏补全 v2.1');
  assert.equal(transcriptionLabel({ engine: 'instrument', revision: 'legato-v2' }), 'BASIC PITCH · 连奏修正 v2');
  assert.equal(transcriptionLabel({ engine: 'instrument' }), 'BASIC PITCH · 主声部跟踪（旧版）');
  assert.equal(transcriptionLabel({ engine: 'instrument', revision: 'future' }), 'BASIC PITCH · future');
  assert.equal(transcriptionLabel({ engine: 'melody' }), '旧版频谱（对照）');
  assert.equal(transcriptionLabel({ engine: 'yin' }), 'YIN · 独奏单音');
  assert.equal(transcriptionLabel(null), '导入谱 / 未记录引擎');
  assert.equal(transcriptionLabel(undefined), '导入谱 / 未记录引擎');
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((onResolve, onReject) => { resolve = onResolve; reject = onReject; });
  return { promise, resolve, reject };
}

test('audition latch is shared across callers and drops contenders without queueing or unlocking', async () => {
  const pending = deferred<string>();
  let calls = 0;
  const first = runAuditionRequest(() => { calls++; return pending.promise; });
  // Fresh callers represent a new panel or a different track/range/kind.
  const nextCaller = () => runAuditionRequest(async () => { calls++; return 'next'; });
  try {
    for (let attempt = 0; attempt < 5; attempt++) {
      assert.deepEqual(await nextCaller(), { status: 'busy' });
      assert.equal(calls, 1);
    }
  } finally {
    pending.resolve('first');
    await first;
  }
  assert.deepEqual(await first, { status: 'done', value: 'first' });
  assert.equal(calls, 1);
  assert.deepEqual(await nextCaller(), { status: 'done', value: 'next' });
  assert.equal(calls, 2);
});

test('audition latch releases only after the owning promise rejects', async () => {
  const pending = deferred<string>();
  const first = runAuditionRequest(() => pending.promise);
  const rejection = assert.rejects(first, /audition failed/);
  try {
    assert.deepEqual(await runAuditionRequest(async () => 'blocked'), { status: 'busy' });
  } finally {
    pending.reject(new Error('audition failed'));
    await rejection;
  }
  assert.deepEqual(await runAuditionRequest(async () => 'retry'), { status: 'done', value: 'retry' });
});

test('audition latch also releases if starting the request throws synchronously', async () => {
  await assert.rejects(runAuditionRequest(() => { throw new Error('bridge unavailable'); }), /bridge unavailable/);
  assert.deepEqual(await runAuditionRequest(async () => 'retry'), { status: 'done', value: 'retry' });
});
