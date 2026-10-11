import assert from "node:assert/strict";
import { nextCaretBoundary } from "../src/textarea-caret.ts";

const text = "甲👩‍💻e\u0301乙";
assert.equal(nextCaretBoundary(text, 1, 1), 6, "a joined emoji must move as one displayed character");
assert.equal(nextCaretBoundary(text, 6, -1), 1);
assert.equal(nextCaretBoundary(text, 6, 1), 8, "a combining accent must stay with its letter");
assert.equal(nextCaretBoundary(text, 8, -1), 6);
assert.equal(nextCaretBoundary(text, 3, 1), 6, "a partial native selection must snap to a valid character boundary");
assert.equal(nextCaretBoundary(text, 3, -1), 1);
assert.equal(nextCaretBoundary(text, 0, -1), 0);
assert.equal(nextCaretBoundary(text, text.length, 1), text.length);
console.log("textarea caret tests passed");
