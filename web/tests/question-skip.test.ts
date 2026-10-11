// A prompt must always offer a way out. The phone had a card with options only,
// so a prompt the user did not want to answer (or one already answered on the
// desktop) forced a choice.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

const server = await createServer({
  server: { middlewareMode: true }, appType: "custom", logLevel: "error",
});
try {
  const { QuestionSheet } = await server.ssrLoadModule(
    "/src/components/QuestionSheet.tsx");

  let skipped = 0;
  const markup = renderToStaticMarkup(createElement(QuestionSheet, {
    question: "继续吗？",
    options: [{ label: "继续" }, { label: "停下" }],
    onAnswer: () => {},
    onSkip: () => { skipped += 1; },
  }));
  assert.match(markup, /qa-skip/, "the dismiss affordance must render");
  assert.match(markup, /跳过/, "it must be labelled in the user's language");
  assert.match(markup, /交给 DSH 处理/,
    "the label must say what dismissing does");

  const withoutSkip = renderToStaticMarkup(createElement(QuestionSheet, {
    question: "继续吗？", options: [{ label: "继续" }], onAnswer: () => {},
  }));
  assert.doesNotMatch(withoutSkip, /qa-skip/,
    "a caller that provides no dismiss must not render a dead button");

  const sending = renderToStaticMarkup(createElement(QuestionSheet, {
    question: "继续吗？", options: [{ label: "继续" }],
    onAnswer: () => {}, onSkip: () => {}, busy: true,
  }));
  assert.match(sending, /正在发送/,
    "an in-flight answer must be visible instead of looking like a dead tap");
  assert.match(sending, /disabled/,
    "options must lock while the answer is in flight");
} finally {
  await server.close();
}

const ws = readFileSync(resolve(process.cwd(), "src/ws.ts"), "utf8");
assert.match(ws, /sendSkipQuestion\(/, "the client must be able to skip");
assert.match(ws, /outcome, sid, ts: nowTs\(\)/,
  "the skip must travel as an outcome on the same command");

const app = readFileSync(resolve(process.cwd(), "src/App.tsx"), "utf8");
assert.match(app, /sendSkipQuestion\(\s*focusedSid, question\.ask_id\)/);
assert.match(app, /onSkip=\{\(\) => \{/, "the sheet must receive the handler");

console.log("question-skip tests passed");

// The card must survive a lost answer: it closes only when the wrapper says so.
const appSource = readFileSync(resolve(process.cwd(), "src/App.tsx"), "utf8");
const answerHandler = appSource.slice(
  appSource.indexOf("onAnswer={(answer) => {"),
  appSource.indexOf("onSkip={() => {"));
assert.doesNotMatch(answerHandler, /type: "answer_question"/,
  "tapping an option must not clear the card optimistically: the answer can "
  + "still be lost in flight, and then nothing is left to press");
assert.match(appSource, /type: "command_error",[\s\S]{0,120}回答未发送/,
  "a refused send must tell the user instead of doing nothing");
assert.match(appSource, /setAskAnswerPending\(question\.ask_id\)/);

// A skip must omit `answer`: the wire schema rejects an empty string, and a
// rejected frame is closed by the relay and then replayed by the outbox on
// every reconnect — an endless reconnect storm, measured in the field.
const wsSource = readFileSync(resolve(process.cwd(), "src/ws.ts"), "utf8");
assert.match(wsSource, /sendAnswerQuestionOutcome\(sid, askId, null, "skip"\)/,
  "a skip must not carry a placeholder answer");
assert.match(wsSource, /if \(answer !== null\) frame\.answer = answer;/,
  "the answer field must be omitted entirely when there is none");
assert.match(wsSource, /msg\.code === "protocol"/,
  "a refused command must be retired from the outbox instead of replayed");
