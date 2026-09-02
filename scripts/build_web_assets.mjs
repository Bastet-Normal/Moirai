import { cpSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const assets = resolve(root, "web", "assets");
const webRoot = resolve(root, "web");

if (assets === webRoot || !relative(webRoot, assets) || relative(webRoot, assets).startsWith("..")) {
  throw new Error("Refusing to rebuild assets outside web/assets");
}

const fontawesomeDir = join(assets, "fontawesome");
const webfontsDir = join(assets, "webfonts");
const licensesDir = join(assets, "licenses");
for (const target of [fontawesomeDir, webfontsDir, licensesDir]) {
  rmSync(target, { recursive: true, force: true });
  mkdirSync(target, { recursive: true });
}

cpSync(
  join(root, "node_modules", "@fortawesome", "fontawesome-free", "css", "all.min.css"),
  join(fontawesomeDir, "all.min.css")
);
cpSync(
  join(root, "node_modules", "@fortawesome", "fontawesome-free", "webfonts"),
  webfontsDir,
  { recursive: true }
);
cpSync(
  join(root, "node_modules", "cytoscape", "dist", "cytoscape.min.js"),
  join(assets, "cytoscape.min.js")
);

const notices = [
  ["tailwindcss.txt", "tailwindcss"],
  ["cytoscape.txt", "cytoscape"],
  ["fontawesome.txt", join("@fortawesome", "fontawesome-free")]
];
for (const [target, pkg] of notices) {
  const source = join(root, "node_modules", pkg, "LICENSE");
  const fallback = join(root, "node_modules", pkg, "LICENSE.txt");
  let license;
  try {
    license = readFileSync(source);
  } catch {
    license = readFileSync(fallback);
  }
  writeFileSync(join(licensesDir, target), license);
}
