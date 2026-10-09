import copy
import json
import os
import shutil
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, StrictInt, ValidationError

from batch_generate import generate_effect, load_key

PROJECT_DIR = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = Path(os.getenv("BATCH_IMAGE_TEMPLATE_DIR", "/Users/leiwanyue/Desktop/效果图"))
OUTPUT_DIR = PROJECT_DIR / "output"
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_BATCH_PRODUCTS = 10
MAX_CONCURRENT_GENERATIONS = 2
ALLOWED_TYPES = {"image/png", "image/jpeg", "image/webp"}
TEMPLATES = {
    "single": {"name": "单罐场景", "filename": "1.png"},
    "double": {"name": "双罐场景", "filename": "2.jpg"},
}
ROLES = ("jar", "herb_overall", "herb_detail")
TERMINAL_STATUSES = {"completed", "partial", "failed"}

app = FastAPI(title="中药主图 AI 批量生成", version="0.3.0")
jobs = {}
jobs_lock = threading.Lock()
generation_slots = threading.Semaphore(MAX_CONCURRENT_GENERATIONS)


class ProductUpload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_name: str
    jar: StrictInt
    herb_overall: StrictInt
    herb_detail: StrictInt


class BatchManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    products: list[ProductUpload]


class RetryItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: str
    template_id: str


class RetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[RetryItem] | None = None


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def has_api_key():
    return bool(os.getenv("DASHSCOPE_API_KEY", "").strip()) or (Path.home() / ".dashscope_api_key").is_file()


def update_job(job_id, **changes):
    with jobs_lock:
        jobs[job_id].update(changes)


def update_product(job_id, product_id, **changes):
    with jobs_lock:
        product = next(item for item in jobs[job_id]["products"] if item["id"] == product_id)
        product.update(changes)


def progress_key(product_id, template_id):
    return f"{product_id}:{template_id}"


def update_unit_progress(job_id, product_id, template_id, stage, message):
    with jobs_lock:
        job = jobs[job_id]
        key = progress_key(product_id, template_id)
        job["_progress"][key] = max(job["_progress"].get(key, 0), stage)
        product = next(item for item in job["products"] if item["id"] == product_id)
        product["completed_steps"] = sum(
            value for unit, value in job["_progress"].items() if unit.startswith(f"{product_id}:")
        )
        product["message"] = message
        job["completed_steps"] = sum(job["_progress"].values())
        job["message"] = message


def public_job(job_id):
    with jobs_lock:
        if job_id not in jobs:
            raise HTTPException(status_code=404, detail="任务不存在")
        job = copy.deepcopy(jobs[job_id])
    job.pop("_progress", None)
    return job


async def save_upload(upload, destination):
    if upload.content_type not in ALLOWED_TYPES:
        raise HTTPException(status_code=400, detail=f"{upload.filename or '文件'} 不是支持的图片格式")
    data = await upload.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail=f"{upload.filename or '文件'} 超过 20MB")
    destination.write_bytes(data)
    try:
        with Image.open(destination) as image:
            image.verify()
    except (UnidentifiedImageError, OSError):
        destination.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=f"{upload.filename or '文件'} 无法识别为有效图片")


def parse_manifest(raw_manifest, upload_count):
    try:
        manifest = BatchManifest.model_validate(json.loads(raw_manifest))
    except (json.JSONDecodeError, ValidationError, TypeError):
        raise HTTPException(status_code=400, detail="上传清单格式错误") from None

    if not manifest.products:
        raise HTTPException(status_code=400, detail="请至少添加一个产品")
    if len(manifest.products) > MAX_BATCH_PRODUCTS:
        raise HTTPException(status_code=400, detail=f"单批最多上传 {MAX_BATCH_PRODUCTS} 个产品")

    normalized = []
    referenced_indices = []
    for item in manifest.products:
        product_name = item.product_name.strip()
        if not product_name:
            raise HTTPException(status_code=400, detail="产品名称不能为空")
        if len(product_name) > 50:
            raise HTTPException(status_code=400, detail=f"{product_name[:20]}… 产品名称不能超过 50 个字符")

        indices = [item.jar, item.herb_overall, item.herb_detail]
        if len(set(indices)) != len(indices):
            raise HTTPException(status_code=400, detail=f"{product_name} 的三类素材不能使用同一张图片")
        if any(index < 0 or index >= upload_count for index in indices):
            raise HTTPException(status_code=400, detail=f"{product_name} 的图片索引无效")

        referenced_indices.extend(indices)
        normalized.append(
            {
                "product_name": product_name,
                "jar": item.jar,
                "herb_overall": item.herb_overall,
                "herb_detail": item.herb_detail,
            }
        )

    if len(set(referenced_indices)) != len(referenced_indices):
        raise HTTPException(status_code=400, detail="不同产品不能重复引用同一张上传图片")
    if set(referenced_indices) != set(range(upload_count)):
        raise HTTPException(status_code=400, detail="上传图片与清单不一致")
    return normalized


def template_sort_key(item):
    return list(TEMPLATES).index(item["template_id"])


def run_generation_unit(job_id, unit, key):
    product_id = unit["product_id"]
    product_name = unit["product_name"]
    template_id = unit["template_id"]
    template = TEMPLATES[template_id]
    output_path = OUTPUT_DIR / job_id / product_id / f"{template_id}.png"

    update_product(job_id, product_id, status="running", message=f"正在生成{template['name']}")

    def on_progress(message, stage):
        update_unit_progress(
            job_id,
            product_id,
            template_id,
            stage - 1,
            f"{product_name} · {template['name']}：{message}",
        )

    try:
        with generation_slots:
            generation = generate_effect(
                key=key,
                jar_path=unit["input_paths"]["jar"],
                herb_overall_path=unit["input_paths"]["herb_overall"],
                herb_detail_path=unit["input_paths"]["herb_detail"],
                template_path=TEMPLATE_DIR / template["filename"],
                output_path=output_path,
                retries=4,
                seed=(int(job_id[:8], 16) + unit["product_index"] * 1009 + unit["template_index"] * 77)
                % 2147483647,
                upscale=True,
                progress=on_progress,
            )
        result = {
            "template_id": template_id,
            "name": template["name"],
            "image_url": f"/api/jobs/{job_id}/products/{product_id}/results/{template_id}",
            "download_url": f"/api/jobs/{job_id}/products/{product_id}/results/{template_id}?download=true",
            "models": {
                "jar": generation["jar_model"],
                "herb": generation["herb_model"],
            },
        }
        with jobs_lock:
            product = next(item for item in jobs[job_id]["products"] if item["id"] == product_id)
            product["results"] = [item for item in product["results"] if item["template_id"] != template_id]
            product["results"].append(result)
            product["errors"] = [item for item in product["errors"] if item["template_id"] != template_id]
    except Exception as exc:
        with jobs_lock:
            product = next(item for item in jobs[job_id]["products"] if item["id"] == product_id)
            product["errors"] = [item for item in product["errors"] if item["template_id"] != template_id]
            product["errors"].append(
                {
                    "template_id": template_id,
                    "name": template["name"],
                    "message": str(exc),
                }
            )
    finally:
        update_unit_progress(
            job_id,
            product_id,
            template_id,
            2,
            f"{product_name} · {template['name']}处理完成",
        )


def finalize_job(job_id):
    with jobs_lock:
        job = jobs[job_id]
        total_results = 0
        total_errors = 0
        for product in job["products"]:
            product["results"].sort(key=template_sort_key)
            product["errors"].sort(key=template_sort_key)
            total_results += len(product["results"])
            total_errors += len(product["errors"])
            if product["results"] and product["errors"]:
                product["status"] = "partial"
                product["message"] = "部分模板生成失败"
            elif product["results"]:
                product["status"] = "completed"
                product["message"] = "全部效果图生成完成"
            else:
                product["status"] = "failed"
                product["message"] = "所有模板均生成失败"
            product["finished_at"] = utc_now()

        if total_results and total_errors:
            job["status"] = "partial"
            job["message"] = "批次处理完成，部分效果图生成失败"
        elif total_results:
            job["status"] = "completed"
            job["message"] = "全部效果图生成完成"
        else:
            job["status"] = "failed"
            job["message"] = "批次生成失败"
        job["completed_steps"] = sum(job["_progress"].values())
        job["error"] = None
        job["finished_at"] = utc_now()
        has_errors = total_errors > 0

    if not has_errors:
        shutil.rmtree(OUTPUT_DIR / job_id / "inputs", ignore_errors=True)


def run_generation_units(job_id, units):
    try:
        key = load_key(None)
        update_job(job_id, status="running", message="正在批量生成", error=None)
        with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_GENERATIONS) as pool:
            futures = [pool.submit(run_generation_unit, job_id, unit, key) for unit in units]
            for future in as_completed(futures):
                future.result()
        finalize_job(job_id)
    except BaseException as exc:
        update_job(job_id, status="failed", message="批次生成失败", error=str(exc), finished_at=utc_now())


def run_generation(job_id, selected_templates, batch_inputs):
    units = []
    for product_index, item in enumerate(batch_inputs):
        for template_index, template_id in enumerate(selected_templates):
            units.append(
                {
                    "product_id": item["id"],
                    "product_name": item["product_name"],
                    "product_index": product_index,
                    "template_id": template_id,
                    "template_index": template_index,
                    "input_paths": item["input_paths"],
                }
            )
    run_generation_units(job_id, units)


@app.get("/api/health")
def health():
    missing_templates = [item["filename"] for item in TEMPLATES.values() if not (TEMPLATE_DIR / item["filename"]).is_file()]
    return {
        "status": "ok" if not missing_templates else "degraded",
        "api_key_configured": has_api_key(),
        "template_dir": str(TEMPLATE_DIR),
        "missing_templates": missing_templates,
    }


@app.get("/api/templates")
def list_templates():
    return [
        {
            "id": template_id,
            "name": item["name"],
            "image_url": f"/api/templates/{template_id}/image",
        }
        for template_id, item in TEMPLATES.items()
    ]


@app.get("/api/templates/{template_id}/image")
def template_image(template_id: str):
    if template_id not in TEMPLATES:
        raise HTTPException(status_code=404, detail="模板不存在")
    path = TEMPLATE_DIR / TEMPLATES[template_id]["filename"]
    if not path.is_file():
        raise HTTPException(status_code=404, detail="模板图片不存在")
    return FileResponse(path)


@app.post("/api/jobs", status_code=202)
async def create_job(
    background_tasks: BackgroundTasks,
    manifest: str = Form(...),
    templates: str = Form("single,double"),
    uploads: list[UploadFile] = File(...),
):
    selected_templates = list(dict.fromkeys(item.strip() for item in templates.split(",") if item.strip()))
    if not selected_templates or any(item not in TEMPLATES for item in selected_templates):
        raise HTTPException(status_code=400, detail="请选择有效模板")
    if not has_api_key():
        raise HTTPException(status_code=503, detail="服务端尚未配置 DashScope API Key")

    products = parse_manifest(manifest, len(uploads))
    job_id = uuid.uuid4().hex
    job_dir = OUTPUT_DIR / job_id
    input_root = job_dir / "inputs"
    input_root.mkdir(parents=True, exist_ok=False)

    batch_inputs = []
    try:
        for product_index, product in enumerate(products, start=1):
            product_id = f"p{product_index:03d}"
            product_input_dir = input_root / product_id
            product_input_dir.mkdir()
            input_paths = {role: product_input_dir / f"{role}.png" for role in ROLES}
            for role in ROLES:
                await save_upload(uploads[product[role]], input_paths[role])
            batch_inputs.append(
                {
                    "id": product_id,
                    "product_name": product["product_name"],
                    "input_paths": input_paths,
                }
            )
    except BaseException:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise

    product_steps = len(selected_templates) * 2
    job = {
        "id": job_id,
        "status": "queued",
        "message": "批次已进入队列",
        "completed_steps": 0,
        "total_steps": len(batch_inputs) * product_steps,
        "templates": selected_templates,
        "products": [
            {
                "id": item["id"],
                "product_name": item["product_name"],
                "status": "queued",
                "message": "等待生成",
                "completed_steps": 0,
                "total_steps": product_steps,
                "results": [],
                "errors": [],
                "finished_at": None,
            }
            for item in batch_inputs
        ],
        "error": None,
        "created_at": utc_now(),
        "finished_at": None,
        "_progress": {
            progress_key(item["id"], template_id): 0
            for item in batch_inputs
            for template_id in selected_templates
        },
    }
    with jobs_lock:
        jobs[job_id] = job
    background_tasks.add_task(run_generation, job_id, selected_templates, batch_inputs)
    return public_job(job_id)


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    return public_job(job_id)


@app.post("/api/jobs/{job_id}/retry", status_code=202)
def retry_failed_job(
    job_id: str,
    background_tasks: BackgroundTasks,
    retry_request: RetryRequest | None = None,
):
    retry_request = retry_request or RetryRequest()
    with jobs_lock:
        if job_id not in jobs:
            raise HTTPException(status_code=404, detail="任务不存在")
        job = jobs[job_id]
        if job["status"] not in TERMINAL_STATUSES:
            raise HTTPException(status_code=409, detail="任务仍在处理中，不能重复提交")

        products_by_id = {product["id"]: product for product in job["products"]}
        failed_items = {
            (product["id"], error["template_id"])
            for product in job["products"]
            for error in product["errors"]
        }
        if not failed_items:
            raise HTTPException(status_code=409, detail="当前任务没有可重试的失败项")

        if retry_request.items:
            requested_items = [(item.product_id, item.template_id) for item in retry_request.items]
            if len(set(requested_items)) != len(requested_items):
                raise HTTPException(status_code=400, detail="重试项不能重复")
            invalid_items = [item for item in requested_items if item not in failed_items]
            if invalid_items:
                raise HTTPException(status_code=400, detail="只能重试当前任务中的失败模板")
        else:
            requested_items = sorted(failed_items)

        units = []
        for product_id, template_id in requested_items:
            product = products_by_id[product_id]
            product_index = job["products"].index(product)
            input_paths = {
                role: OUTPUT_DIR / job_id / "inputs" / product_id / f"{role}.png"
                for role in ROLES
            }
            if any(not path.is_file() for path in input_paths.values()):
                raise HTTPException(status_code=410, detail=f"{product['product_name']} 的原始上传图片已不存在")

            product["errors"] = [
                error for error in product["errors"] if error["template_id"] != template_id
            ]
            product["status"] = "queued"
            product["message"] = f"等待重试{TEMPLATES[template_id]['name']}"
            product["finished_at"] = None
            job["_progress"][progress_key(product_id, template_id)] = 0
            units.append(
                {
                    "product_id": product_id,
                    "product_name": product["product_name"],
                    "product_index": product_index,
                    "template_id": template_id,
                    "template_index": list(TEMPLATES).index(template_id),
                    "input_paths": input_paths,
                }
            )

        for product in job["products"]:
            product["completed_steps"] = sum(
                value
                for unit, value in job["_progress"].items()
                if unit.startswith(f"{product['id']}:")
            )
        job["completed_steps"] = sum(job["_progress"].values())
        job["status"] = "queued"
        job["message"] = f"{len(units)} 个失败项已进入重试队列"
        job["error"] = None
        job["finished_at"] = None

    background_tasks.add_task(run_generation_units, job_id, units)
    return public_job(job_id)


@app.get("/api/jobs/{job_id}/products/{product_id}/results/{template_id}")
def get_result(job_id: str, product_id: str, template_id: str, download: bool = False):
    job = public_job(job_id)
    product = next((item for item in job["products"] if item["id"] == product_id), None)
    if product is None:
        raise HTTPException(status_code=404, detail="该任务没有此产品")
    result = next((item for item in product["results"] if item["template_id"] == template_id), None)
    if result is None:
        raise HTTPException(status_code=404, detail="该产品没有此模板结果")
    path = OUTPUT_DIR / job_id / product_id / f"{template_id}.png"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="效果图尚未生成")
    filename = f"{product['product_name']}-{TEMPLATES[template_id]['name']}.png"
    return FileResponse(path, media_type="image/png", filename=filename if download else None)
