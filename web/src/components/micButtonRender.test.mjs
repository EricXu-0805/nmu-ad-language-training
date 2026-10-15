import assert from 'node:assert/strict';
import test from 'node:test';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer } from 'vite';

test('microphone errors override stale armed states, and controls retain noninteractive space', async t => {
  const vite = await createServer({ root: process.cwd(), appType: 'custom', logLevel: 'silent', server: { middlewareMode: true } });
  t.after(() => vite.close());
  const { MicButton, MicButtonSpace } = await vite.ssrLoadModule('/src/components/MicButton.tsx');
  const render = props => renderToStaticMarkup(React.createElement(MicButton, { onStart() {}, onStop() {}, ...props }));
  for (const state of ['armed', 'recording']) {
    const markup = render({ state, micError: true, starting: true, selfStart: true });
    assert.match(markup, /麦克风没有打开/); assert.doesNotMatch(markup, /正在准备麦克风|<button/);
  }
  for (const props of [{ state: 'idle' }, { state: 'armed' }, { state: 'idle', selfStart: true }, { state: 'recording', localActive: true }]) {
    const markup = render(props); assert.equal((markup.match(/patient-control-row /g) ?? []).length, 3);
    assert.equal((markup.match(/patient-layout-reserve/g) ?? []).length, 3);
    assert.equal((markup.match(/<button/g) ?? []).length, props.selfStart || props.localActive ? 1 : 0);
  }
  const active = render({ state: 'armed', micError: true, localActive: true });
  assert.match(active, /麦克风已打开/); assert.match(active, /我说好了/);
  const space = renderToStaticMarkup(React.createElement(MicButtonSpace));
  assert.doesNotMatch(space, /<button|tabindex|role="status"/); assert.match(space, /aria-hidden="true"/);
});
