// 运行：node --experimental-strip-types tests/spans.test.ts
import assert from "node:assert/strict";
import { spliceSpans, type Span } from "../src/spans.ts";

const text = (s: Span[]) => s.map((x) => x.text).join("");
let s: Span[] = [];
s = spliceSpans(s, 0, 0, "Hello", "human");
s = spliceSpans(s, 5, 5, " world", "human");
assert.deepEqual(s, [{ text: "Hello world", origin: "human" }]);
s = spliceSpans(s, 5, 5, " PASTE", "pasted");
assert.equal(text(s), "Hello PASTE world");
assert.deepEqual(s.map((x) => x.origin), ["human", "pasted", "human"]);
s = spliceSpans(s, 0, 8, "", "human"); // 删除跨越两个片段
assert.equal(text(s), "STE world");
assert.deepEqual(s.map((x) => [x.text, x.origin]), [["STE", "pasted"], [" world", "human"]]);
s = spliceSpans(s, 3, 3, "!", "human"); // 在片段边界插入
assert.equal(text(s), "STE! world");
s = spliceSpans(s, 0, text(s).length, "new", "human"); // 全选替换
assert.deepEqual(s, [{ text: "new", origin: "human" }]);
console.log("spans ok");
