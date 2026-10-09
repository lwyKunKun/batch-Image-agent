#!/usr/bin/env python3
"""批量生成产品场景图（DashScope Qwen-Image-Edit 版）。

每个产品使用三张参考图：
- 罐装主图：约束罐体、标签和罐内产品
- 药材整体图：约束前景瓷盘中的堆放形态
- 药材细节图：补充药材的颜色、切面和纹理

生成分为两阶段：先替换模板中的罐子，再替换瓷盘中的药材。核心函数同时供
命令行和 Web API 使用。
"""
import argparse
import base64
import io
import json
import os
import random
import ssl
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from PIL import Image

BASE_DIR = Path("/Users/leiwanyue/Desktop/蒋欣伟")
TEMPLATE_DIR = Path("/Users/leiwanyue/Desktop/效果图")
OUT_DIR = Path("/Users/leiwanyue/Desktop/产品图批量生成/输出")
# (模板文件名, 输出后缀)
TEMPLATES = [("1.png", "单罐"), ("2.jpg", "双罐")]
API_URL = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
# 两阶段都把替换目标放图1作锚点；max 对标签版式和药材个体形状的还原都明显更强，
# 不可用时逐级回退。
MODELS_JAR = ["qwen-image-edit-max", "qwen-image-edit-plus", "qwen-image-edit"]
MODELS_BOWL = ["qwen-image-edit-max", "qwen-image-edit-plus", "qwen-image-edit"]
MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
FORMAT_MIME = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}
RETRYABLE_HTTP_CODES = {429, 500, 502, 503, 504}
try:
    import certifi

    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CTX = ssl.create_default_context()

PROMPT_JAR = (
    "专业电商产品摄影修图任务。图1是目标产品罐渲染图，图2是场景底图。"
    "只替换图2已有的罐子，不得新增、复制或减少罐子；罐子数量、位置、尺寸、遮挡关系必须与图2完全一致。"
    "把图1的罐子产品原样合成进图2已有的每一个罐子位置："
    "输出中罐子的标签（底色、版式、边框、logo、全部中文字）必须与图1完全一致、原样再现，"
    "图2原有的白底绿框「山药」标签不得以任何形式残留；罐形与黑色盖子保持图2的样式；"
    "罐身所有透明处（标签上方和下方）都露出图1罐内所装的产品，不得残留图2的白色切片。"
    "除罐子内容外，图2的一切保持不变：场景、相机角度、背景道具、光线氛围、构图和瓷盘。"
    "再次强调：图2原来只有一个罐子就必须只输出一个，原来有两个就必须只输出两个。"
    "输出必须照片级真实，阴影、反光、景深与场景一致。"
)

PROMPT_BOWL = (
    "专业电商产品摄影修图任务。图1是目标药材整体参考图，图2是场景底图，图3是同一药材的细节参考图。"
    "把图2前景瓷盘里原有的全部白色山药片完整替换成目标药材，不得残留任何白色山药片。"
    "药材的单体形状、大小、数量密度和堆放效果严格以图1为准；图3只用于补充颜色、切面和纹理，"
    "绝对不得把图3中的微距单片按特写比例放进场景，不得出现异常巨大的单片药材。"
    "只取药材本身，不得带入参考图里的盘子、勺子、桌面、文字或背景。"
    "图2原有瓷盘必须原样保留，釉色、纹理、盘形和盘沿颜色一律不得改变，只改变盘中货物。"
    "除盘中货物外，图2的一切保持不变：场景、构图、罐子数量与位置、罐身标签、背景道具、光线和景深。"
    "输出必须照片级真实，所有药材尺寸符合真实比例，阴影和透视与场景一致。"
)


def load_key(arg_key):
    if arg_key:
        return arg_key.strip()
    if os.environ.get("DASHSCOPE_API_KEY"):
        return os.environ["DASHSCOPE_API_KEY"].strip()
    p = Path.home() / ".dashscope_api_key"
    if p.exists():
        return p.read_text().strip()
    sys.exit("未找到 API Key：请 export DASHSCOPE_API_KEY=sk-... 或写入 ~/.dashscope_api_key")


def to_data_url(src):
    if isinstance(src, Image.Image):
        buffer = io.BytesIO()
        src.save(buffer, format="PNG")
        encoded = buffer.getvalue()
        mime = "image/png"
    else:
        encoded = bytes(src) if isinstance(src, (bytes, bytearray)) else Path(src).read_bytes()
        with Image.open(io.BytesIO(encoded)) as image:
            mime = FORMAT_MIME.get(image.format)
        if mime is None:
            raise ValueError("仅支持 PNG、JPEG 和 WebP 图片")
    return f"data:{mime};base64,{base64.b64encode(encoded).decode()}"


def whiteness(path):
    # 白底像素占比：俯拍盘图有一圈白瓷边，比例高；特写图背景深，比例低
    with Image.open(path) as im:
        im = im.convert("RGB")
        im.thumbnail((128, 128))
        px = list(im.getdata())
    white = sum(1 for r, g, b in px if r > 230 and g > 230 and b > 230)
    return white / len(px)


def collect_refs(cat_dir):
    imgs = [p for p in cat_dir.iterdir() if p.suffix.lower() in MIME]
    if len(imgs) < 3:
        raise ValueError(f"{cat_dir.name} 至少需要 3 张图片")
    jars = [p for p in imgs if "罐" in p.name]
    jar = jars[0] if jars else max(imgs, key=lambda p: p.stat().st_size)
    herbs = sorted((p for p in imgs if p != jar), key=whiteness, reverse=True)
    return jar, herbs[0], herbs[-1]


def call_api(key, model, images, prompt, seed=None, extra_params=None):
    content = [{"image": to_data_url(p)} for p in images]
    content.append({"text": prompt})
    # prompt_extend 默认 true 会让模型改写指令，导致约束丢失，必须关闭
    params = {"n": 1, "watermark": False, "prompt_extend": False}
    if seed is not None:
        params["seed"] = seed
    if extra_params:
        params.update(extra_params)
    body = json.dumps(
        {
            "model": model,
            "input": {"messages": [{"role": "user", "content": content}]},
            "parameters": params,
        }
    ).encode()
    req = urllib.request.Request(
        API_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=600, context=SSL_CTX) as resp:
        data = json.load(resp)
    for item in data["output"]["choices"][0]["message"]["content"]:
        if "image" in item:
            with urllib.request.urlopen(item["image"], timeout=300, context=SSL_CTX) as r:
                return r.read()
    raise RuntimeError(f"响应中没有图片: {json.dumps(data, ensure_ascii=False)[:300]}")


def upscale_to(img_bytes, target_path):
    # 把模型输出放大回模板原始尺寸，电商主图够用
    im = Image.open(io.BytesIO(img_bytes))
    with Image.open(target_path) as tpl:
        size = tpl.size
    if im.size != size:
        im = im.resize(size, Image.LANCZOS)
    out = io.BytesIO()
    im.save(out, format="PNG")
    return out.getvalue()


def retry_delay(attempt):
    time.sleep(min(2**attempt, 30) + random.uniform(0, 1))


def call_with_retry(key, args, images, prompt, seed, extra_params=None, models=None):
    last_err = None
    for model in models or MODELS_JAR:
        current_seed = seed
        current_params = dict(extra_params or {})
        model_unavailable = False

        for attempt in range(1, args.retries + 1):
            detail = ""
            try:
                while True:
                    try:
                        return call_api(
                            key,
                            model,
                            images,
                            prompt,
                            current_seed,
                            current_params or None,
                        ), model
                    except urllib.error.HTTPError as error:
                        detail = error.read()[:300].decode("utf-8", "replace")
                        if error.code == 400 and "seed" in detail.lower() and current_seed is not None:
                            current_seed = None
                            continue
                        if error.code == 400 and "size" in detail.lower() and "size" in current_params:
                            current_params.pop("size")
                            continue
                        raise
            except urllib.error.HTTPError as error:
                last_err = f"HTTP {error.code}: {detail}"
                lower_detail = detail.lower()
                if error.code in (400, 404) and "model" in lower_detail:
                    model_unavailable = True
                    break
                if error.code not in RETRYABLE_HTTP_CODES:
                    raise RuntimeError(last_err) from error
            except (urllib.error.URLError, TimeoutError, ConnectionError, ssl.SSLError) as error:
                last_err = f"{type(error).__name__}: {error}"
            except Exception as error:
                raise RuntimeError(f"{type(error).__name__}: {error}") from error

            if attempt < args.retries:
                retry_delay(attempt)

        if model_unavailable:
            continue

    raise RuntimeError(last_err or "图片生成请求失败")


def generate_effect(
    key,
    jar_path,
    herb_overall_path,
    herb_detail_path,
    template_path,
    output_path,
    retries=4,
    seed=1000,
    upscale=True,
    progress=None,
):
    """使用三张产品参考图生成一张指定模板的效果图。"""
    template_path = Path(template_path)
    output_path = Path(output_path)
    args = argparse.Namespace(retries=retries)
    with Image.open(template_path) as template:
        extra = {"size": f"{template.width}*{template.height}"}

    if progress:
        progress("正在替换罐体", 1)
    stage1, jar_model = call_with_retry(
        key,
        args,
        [jar_path, template_path],
        PROMPT_JAR,
        seed=seed,
        extra_params=extra,
        models=MODELS_JAR,
    )

    if progress:
        progress("正在替换盘中药材", 2)
    final, herb_model = call_with_retry(
        key,
        args,
        [herb_overall_path, stage1, herb_detail_path],
        PROMPT_BOWL,
        seed=seed + 1,
        extra_params=extra,
        models=MODELS_BOWL,
    )
    if upscale:
        final = upscale_to(final, template_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(final)
    return {"path": output_path, "jar_model": jar_model, "herb_model": herb_model}


def run_job(job, key, args):
    cat, suffix, tpl_path, stems = job
    jar, herb_overall, herb_detail = collect_refs(BASE_DIR / cat)
    done, failed = [], []
    for i, stem in enumerate(stems):
        out_path = OUT_DIR / cat / f"{stem}.png"
        if out_path.exists() and not args.force:
            done.append(out_path.name)
            continue
        try:
            generate_effect(
                key,
                jar,
                herb_overall,
                herb_detail,
                tpl_path,
                out_path,
                retries=args.retries,
                seed=1000 + i * 77,
                upscale=args.upscale,
            )
            done.append(out_path.name)
        except Exception as e:
            failed.append(f"{stem}: {e}")
    msg = f"[完成] {cat} {suffix} {len(done)} 张: {', '.join(done)}"
    if failed:
        msg += f" | 失败: {'; '.join(failed)}"
    return msg


def main():
    ap = argparse.ArgumentParser(description="批量生成产品场景图（DashScope）")
    ap.add_argument("--only", help="只跑指定分类，如：--only 丹参")
    ap.add_argument("--candidates", type=int, default=1, help="每个任务生成几个候选")
    ap.add_argument("--workers", type=int, default=2, help="并发数，默认 2")
    ap.add_argument("--retries", type=int, default=4)
    ap.add_argument("--upscale", action="store_true", help="放大回模板原始尺寸")
    ap.add_argument("--force", action="store_true", help="覆盖已存在的输出")
    ap.add_argument("--key", help="API Key（默认读环境变量或 ~/.dashscope_api_key）")
    args = ap.parse_args()

    key = load_key(args.key)
    jobs = []
    for cat_dir in sorted(p for p in BASE_DIR.iterdir() if p.is_dir()):
        if args.only and cat_dir.name != args.only:
            continue
        for tpl_name, suffix in TEMPLATES:
            stems = []
            for c in range(args.candidates):
                stems.append(
                    f"{cat_dir.name}_{suffix}"
                    if args.candidates == 1
                    else f"{cat_dir.name}_{suffix}_c{c + 1}"
                )
            jobs.append((cat_dir.name, suffix, TEMPLATE_DIR / tpl_name, stems))
    if not jobs:
        print("没有需要生成的任务")
        return
    total = sum(len(s[3]) for s in jobs)
    print(f"共 {len(jobs)} 个任务 / {total} 张候选，并发={args.workers}")
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(run_job, j, key, args): j for j in jobs}
        for fut in as_completed(futs):
            print(fut.result(), flush=True)


if __name__ == "__main__":
    main()
