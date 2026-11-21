// Render Markdown Mermaid sources with an existing local toolchain; no downloads.
// Usage: node ops/render_docs_diagrams.cjs --output DIR --screenshots DIR
//   --puppeteer-module PATH --mermaid-bundle PATH --chrome PATH [--date YYYY-MM-DD] FILE.md [...]
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const root = process.cwd();
const options = {};
const files = [];
for (let i = 2; i < process.argv.length; ++i) {
  const arg = process.argv[i];
  if (arg.startsWith('--')) {
    if (!['--output', '--screenshots', '--puppeteer-module', '--mermaid-bundle', '--chrome', '--date'].includes(arg)
        || !process.argv[i + 1]) throw new Error('Unknown or incomplete renderer option');
    options[arg.slice(2)] = process.argv[++i];
  } else files.push(arg);
}
for (const key of ['output', 'screenshots', 'puppeteer-module', 'mermaid-bundle', 'chrome']) {
  if (!options[key]) throw new Error(`Required: --${key}`);
}
if (!files.length) throw new Error('At least one Markdown file is required');
const generatedOn = options.date || new Date().toISOString().slice(0, 10);
if (!/^\d{4}-\d{2}-\d{2}$/.test(generatedOn)
    || new Date(generatedOn).toISOString().slice(0, 10) !== generatedOn) throw new Error('Invalid date');
const puppeteer = require(path.resolve(options['puppeteer-module']));
const output = path.resolve(options.output);
const screenshots = path.resolve(options.screenshots);
fs.mkdirSync(output, { recursive: true });
fs.mkdirSync(screenshots, { recursive: true });
const sha = value => crypto.createHash('sha256').update(value).digest('hex');
const relative = value => path.relative(root, value).split(path.sep).join('/');
const xmlEscape = value => value.replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;');

(async () => {
  const browser = await puppeteer.launch({
    executablePath: path.resolve(options.chrome), headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-background-networking'],
  });
  const results = [];
  try {
    const page = await browser.newPage();
    await page.setRequestInterception(true);
    page.on('request', request => /^(about:|data:)/.test(request.url()) ? request.continue() : request.abort());
    await page.setViewport({ width: 1800, height: 2400, deviceScaleFactor: 1 });
    await page.setContent('<!doctype html><html><head><meta charset="utf-8"><style>body{margin:24px;background:#fff;font-family:Arial,sans-serif}svg{max-width:100%;height:auto}#drawing{max-width:1752px}</style></head><body><main id="drawing"></main></body></html>');
    await page.addScriptTag({ path: path.resolve(options['mermaid-bundle']) });
    for (const file of files) {
      const markdown = fs.readFileSync(file, 'utf8');
      let index = 0;
      for (const match of markdown.matchAll(/^```mermaid[^\n]*\n([\s\S]*?)^```\s*$/gm)) {
        ++index;
        const source = match[1];
        const stem = file.replace(/[^a-zA-Z0-9]/g, '_') + '_' + index;
        const sourceHash = sha(source);
        const rendered = await page.evaluate(async ({ source, id }) => {
          mermaid.initialize({ startOnLoad: false, securityLevel: 'strict', theme: 'neutral',
            fontFamily: 'Arial, sans-serif', fontSize: 16, deterministicIds: true, deterministicIDSeed: id,
            flowchart: { useMaxWidth: false, nodeSpacing: 30, rankSpacing: 40 },
            sequence: { useMaxWidth: false, actorMargin: 30 }, state: { useMaxWidth: false } });
          document.getElementById('drawing').innerHTML = '';
          const result = await mermaid.render(id, source);
          document.getElementById('drawing').innerHTML = result.svg;
          await document.fonts.ready;
          const svg = document.querySelector('#drawing svg');
          const box = svg.getBoundingClientRect();
          const clipped = [...svg.querySelectorAll('text,foreignObject')].filter(element => {
            const label = element.getBoundingClientRect();
            return label.width > 0 && label.height > 0 && (label.left < box.left - 3
              || label.right > box.right + 3 || label.top < box.top - 3 || label.bottom > box.bottom + 3);
          }).map(element => element.textContent.trim().slice(0, 160));
          // Mermaid output may contain HTML-style <br> in foreignObject labels.
          // Serialize the parsed SVG DOM as XML so standalone SVG readers work.
          return { svg: new XMLSerializer().serializeToString(svg), width: box.width, height: box.height,
            nodes: svg.querySelectorAll('.node').length,
            edges: svg.querySelectorAll('.flowchart-link').length, out_of_bounds_labels: clipped };
        }, { source, id: 'docs_' + stem });
        const metadata = `<metadata>source=${xmlEscape(file)}#${index}; sha256=${sourceHash}; generated=${generatedOn}</metadata>`;
        const svg = rendered.svg.replace(/(<svg\b[^>]*>)/, '$1' + metadata);
        const svgFile = path.join(output, stem + '.svg');
        const sourceFile = path.join(output, stem + '.mmd');
        fs.writeFileSync(svgFile, svg);
        fs.writeFileSync(sourceFile, source);
        await (await page.$('#drawing svg')).screenshot({ path: path.join(screenshots, stem + '.png') });
        results.push({ file, index, source_sha256: sourceHash, status: 'rendered_source_verified',
          svg: relative(svgFile), svg_sha256: sha(svg), source: relative(sourceFile),
          width: rendered.width, height: rendered.height, nodes: rendered.nodes, edges: rendered.edges,
          out_of_bounds_labels: rendered.out_of_bounds_labels });
        process.stdout.write(`${file}#${index}: ${Math.round(rendered.width)}x${Math.round(rendered.height)}, clipped labels=${rendered.out_of_bounds_labels.length}\n`);
      }
    }
    fs.writeFileSync(path.join(output, 'manifest.json'), JSON.stringify({
      generated_on: generatedOn, mode: 'static source-backed architecture diagrams',
      renderer: 'Local Mermaid bundle + headless Chrome; external requests blocked',
      generator: 'ops/render_docs_diagrams.cjs', count: results.length, results,
    }, null, 2) + '\n');
  } finally { await browser.close(); }
})().catch(error => { process.stderr.write(String(error) + '\n'); process.exitCode = 1; });
