// vmc-harness 내장 TS 스캐너 — 고객 프로젝트 node_modules의 typescript 컴파일러 API를 쓴다(추가 설치 없음).
// 입력(stdin JSON): {"root": "...", "files": ["rel/a.ts", ...], "entry_re": "...", "test_re": "..."}
// 출력(stdout JSON): {"unreferenced": [{"path","symbol","kind":"symbol|file"}], "functions": [{"path","symbol","start","end","lines"}]}
"use strict";
const fs = require("fs");
const path = require("path");

function readStdin() {
  return fs.readFileSync(0, "utf8");
}

function main() {
  const input = JSON.parse(readStdin());
  const root = input.root;
  let ts;
  try {
    ts = require(require.resolve("typescript", { paths: [root] }));
  } catch (e) {
    process.stdout.write(JSON.stringify({ error: "typescript 없음(node_modules)" }));
    return;
  }
  const entryRe = new RegExp(input.entry_re);
  const testRe = new RegExp(input.test_re);
  const rel = (abs) => path.relative(root, abs).split(path.sep).join("/");
  const absFiles = input.files.map((f) => path.join(root, f));

  let options = { allowJs: true, jsx: ts.JsxEmit.Preserve, noEmit: true, skipLibCheck: true, esModuleInterop: true };
  const cfgPath = ts.findConfigFile(root, ts.sys.fileExists, "tsconfig.json");
  if (cfgPath && path.dirname(cfgPath) === root) {
    const cfg = ts.readConfigFile(cfgPath, ts.sys.readFile);
    if (!cfg.error) {
      const parsed = ts.parseJsonConfigFileContent(cfg.config, ts.sys, root);
      options = Object.assign({}, parsed.options, { allowJs: true, noEmit: true, skipLibCheck: true });
    }
  }
  const program = ts.createProgram(absFiles, options);
  const checker = program.getTypeChecker();
  const fileSet = new Set(absFiles.map((f) => path.resolve(f)));
  const sources = program.getSourceFiles().filter((sf) => fileSet.has(path.resolve(sf.fileName)));

  // 1) 심볼 참조 수
  const refCount = new Map();
  const declNames = new Set();
  const decls = [];
  const hasExport = (node) => !!(node.modifiers && node.modifiers.some((m) => m.kind === ts.SyntaxKind.ExportKeyword));
  for (const sf of sources) {
    for (const st of sf.statements) {
      const push = (nameNode, kind) => {
        if (!nameNode || !ts.isIdentifier(nameNode)) return;
        declNames.add(nameNode);
        decls.push({ sf, nameNode, kind, exported: hasExport(st) });
      };
      if (ts.isFunctionDeclaration(st) || ts.isClassDeclaration(st) || ts.isInterfaceDeclaration(st) ||
          ts.isTypeAliasDeclaration(st) || ts.isEnumDeclaration(st)) {
        push(st.name, "symbol");
      } else if (ts.isVariableStatement(st)) {
        for (const d of st.declarationList.declarations) push(d.name, "symbol");
      }
    }
  }
  const resolve = (s) => {
    if (s && (s.flags & ts.SymbolFlags.Alias)) {
      try { return checker.getAliasedSymbol(s); } catch (e) { return s; }
    }
    return s;
  };
  for (const sf of sources) {
    const visit = (node) => {
      if (ts.isIdentifier(node) && !declNames.has(node)) {
        let s;
        try { s = resolve(checker.getSymbolAtLocation(node)); } catch (e) { s = undefined; }
        if (s) refCount.set(s, (refCount.get(s) || 0) + 1);
        // export { a as b } / shorthand 속성 등은 위에서 별칭으로 풀린다
        if (node.parent && ts.isShorthandPropertyAssignment(node.parent)) {
          try {
            const vs = checker.getShorthandAssignmentValueSymbol(node.parent);
            if (vs) refCount.set(vs, (refCount.get(vs) || 0) + 1);
          } catch (e) { /* 무시 */ }
        }
      }
      ts.forEachChild(node, visit);
    };
    visit(sf);
  }
  const unreferenced = [];
  for (const d of decls) {
    const r = rel(d.sf.fileName);
    if (entryRe.test(r) || testRe.test(r)) continue;
    let s;
    try { s = checker.getSymbolAtLocation(d.nameNode); } catch (e) { s = undefined; }
    if (!s) continue;
    // 재귀(자기 참조)도 참조로 센다 — 보수적(제거 후보를 덜 낸다)
    const count = refCount.get(s) || 0;
    if (count === 0) unreferenced.push({ path: r, symbol: d.nameNode.text, kind: "symbol" });
  }

  // 2) 아무도 import하지 않는 파일
  const imported = new Set();
  const host = ts.sys;
  for (const sf of sources) {
    const specs = [];
    const visit = (node) => {
      if ((ts.isImportDeclaration(node) || ts.isExportDeclaration(node)) && node.moduleSpecifier && ts.isStringLiteral(node.moduleSpecifier)) {
        specs.push(node.moduleSpecifier.text);
      } else if (ts.isCallExpression(node) && node.arguments.length === 1 && ts.isStringLiteral(node.arguments[0]) &&
                 (node.expression.kind === ts.SyntaxKind.ImportKeyword || (ts.isIdentifier(node.expression) && node.expression.text === "require"))) {
        specs.push(node.arguments[0].text);
      }
      ts.forEachChild(node, visit);
    };
    visit(sf);
    for (const spec of specs) {
      const res = ts.resolveModuleName(spec, sf.fileName, options, host).resolvedModule;
      if (res && res.resolvedFileName) imported.add(path.resolve(res.resolvedFileName));
    }
  }
  for (const sf of sources) {
    const r = rel(sf.fileName);
    if (entryRe.test(r) || testRe.test(r)) continue;
    if (!imported.has(path.resolve(sf.fileName))) unreferenced.push({ path: r, symbol: null, kind: "file" });
  }

  // 3) 함수 길이(이름 있는 것)
  const functions = [];
  for (const sf of sources) {
    const r = rel(sf.fileName);
    const lineOf = (pos) => sf.getLineAndCharacterOfPosition(pos).line + 1;
    const visit = (node, cls) => {
      let name = null;
      if ((ts.isFunctionDeclaration(node) || ts.isMethodDeclaration(node)) && node.name && node.body) {
        name = (cls ? cls + "." : "") + node.name.getText(sf);
      } else if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name) && node.initializer &&
                 (ts.isArrowFunction(node.initializer) || ts.isFunctionExpression(node.initializer))) {
        name = node.name.text;
      }
      if (name) {
        const start = lineOf(node.getStart(sf));
        const end = lineOf(node.getEnd());
        functions.push({ path: r, symbol: name, start, end, lines: end - start + 1 });
      }
      const nextCls = ts.isClassDeclaration(node) && node.name ? node.name.text : cls;
      ts.forEachChild(node, (c) => visit(c, nextCls));
    };
    visit(sf, null);
  }
  process.stdout.write(JSON.stringify({ unreferenced, functions }));
}

try {
  main();
} catch (e) {
  process.stdout.write(JSON.stringify({ error: String((e && e.message) || e).slice(0, 300) }));
}
