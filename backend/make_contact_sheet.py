#!/usr/bin/env python3
"""把输出目录里的候选图生成为一个 HTML 对比挑选页。

每个（分类 × 单/双罐）一行，候选图并排显示，点击选中，
「复制挑选结果」按钮把 JSON 复制到剪贴板，粘贴给 Claude 即可应用。
"""
import json
from pathlib import Path

OUT_DIR = Path("/Users/leiwanyue/Desktop/产品图批量生成/输出")
HTML_PATH = Path("/Users/leiwanyue/Desktop/产品图批量生成/挑选.html")

groups = {}
for p in sorted(OUT_DIR.rglob("*.png")):
    if "_c" not in p.stem:
        continue
    key = p.stem.rsplit("_c", 1)[0]
    groups.setdefault(key, []).append(p)

rows = []
for key, files in groups.items():
    cells = []
    for f in files:
        rel = f.relative_to(HTML_PATH.parent).as_posix()
        cells.append(
            f'<div class="cand" data-key="{key}" data-file="{f.name}" onclick="pick(this)">'
            f'<img src="{rel}"><div class="tag">{f.stem.split("_")[-1]}</div></div>'
        )
    rows.append(f'<section><h2>{key}</h2><div class="row">{"".join(cells)}</div></section>')

html = f"""<!doctype html>
<meta charset="utf-8">
<title>候选图挑选</title>
<style>
body {{ font-family: sans-serif; margin: 24px; background: #fafafa; }}
.row {{ display: flex; gap: 12px; flex-wrap: wrap; }}
.cand {{ position: relative; cursor: pointer; border: 3px solid transparent; border-radius: 8px; }}
.cand img {{ width: 360px; border-radius: 6px; display: block; }}
.cand.sel {{ border-color: #e4393c; }}
.tag {{ position: absolute; top: 6px; left: 6px; background: rgba(0,0,0,.6); color: #fff;
       padding: 2px 8px; border-radius: 4px; font-size: 13px; }}
button {{ margin: 16px 0; padding: 8px 20px; font-size: 15px; }}
#stat {{ color: #666; margin-left: 12px; }}
</style>
<h1>候选图挑选（点击选中，每组选一张）</h1>
<button onclick="copyPicks()">复制挑选结果</button><span id="stat"></span>
{''.join(rows)}
<script>
const picks = {{}};
function pick(el) {{
  const key = el.dataset.key;
  document.querySelectorAll(`.cand[data-key="${{key}}"]`).forEach(c => c.classList.remove('sel'));
  el.classList.add('sel');
  picks[key] = el.dataset.file;
  document.getElementById('stat').textContent = `已选 ${{Object.keys(picks).length}} / {len(groups)} 组`;
}}
function copyPicks() {{
  navigator.clipboard.writeText(JSON.stringify(picks, null, 0));
  alert('已复制，粘贴给 Claude 即可');
}}
</script>
"""
HTML_PATH.write_text(html)
print(f"已生成 {HTML_PATH}，共 {len(groups)} 组待挑选")
