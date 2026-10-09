import io
import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import app as server


def image_bytes(color="green"):
    buffer = io.BytesIO()
    Image.new("RGB", (12, 12), color).save(buffer, format="PNG")
    return buffer.getvalue()


def batch_manifest(names=("丹参片", "黄芪片")):
    products = []
    for index, name in enumerate(names):
        base = index * 3
        products.append(
            {
                "product_name": name,
                "jar": base,
                "herb_overall": base + 1,
                "herb_detail": base + 2,
            }
        )
    return {"products": products}


def upload_files(count):
    return [
        ("uploads", (f"image-{index}.png", image_bytes((index * 20 % 255, 100, 80)), "image/png"))
        for index in range(count)
    ]


@pytest.fixture
def client(tmp_path, monkeypatch):
    output_dir = tmp_path / "output"
    template_dir = tmp_path / "templates"
    template_dir.mkdir()
    Image.new("RGB", (12, 12), "white").save(template_dir / "1.png")
    Image.new("RGB", (12, 12), "white").save(template_dir / "2.jpg")

    monkeypatch.setattr(server, "OUTPUT_DIR", output_dir)
    monkeypatch.setattr(server, "TEMPLATE_DIR", template_dir)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    server.jobs.clear()

    def fake_generate_effect(*, output_path, progress, **kwargs):
        progress("正在替换罐体", 1)
        progress("正在替换盘中药材", 2)
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (12, 12), "blue").save(output_path)
        return {"jar_model": "mock-jar", "herb_model": "mock-herb"}

    monkeypatch.setattr(server, "generate_effect", fake_generate_effect)
    with TestClient(server.app) as test_client:
        yield test_client, output_dir
    server.jobs.clear()


def create_batch(client, manifest=None, files=None, templates="single,double"):
    manifest = manifest or batch_manifest()
    files = files or upload_files(len(manifest["products"]) * 3)
    return client.post(
        "/api/jobs",
        data={"manifest": json.dumps(manifest, ensure_ascii=False), "templates": templates},
        files=files,
    )


def test_creates_two_product_batch_and_serves_results(client):
    test_client, output_dir = client
    response = create_batch(test_client)

    assert response.status_code == 202
    job_id = response.json()["id"]
    job = test_client.get(f"/api/jobs/{job_id}").json()

    assert job["status"] == "completed"
    assert job["completed_steps"] == job["total_steps"] == 8
    assert [product["product_name"] for product in job["products"]] == ["丹参片", "黄芪片"]
    assert all(product["status"] == "completed" for product in job["products"])
    assert all(len(product["results"]) == 2 for product in job["products"])
    assert not (output_dir / job_id / "inputs").exists()

    result = test_client.get(f"/api/jobs/{job_id}/products/p001/results/single")
    assert result.status_code == 200
    assert result.headers["content-type"] == "image/png"
    assert test_client.get(f"/api/jobs/{job_id}/products/p999/results/single").status_code == 404
    assert test_client.get(f"/api/jobs/{job_id}/products/p001/results/missing").status_code == 404


@pytest.mark.parametrize(
    ("raw_manifest", "expected_detail"),
    [
        ("not-json", "上传清单格式错误"),
        (json.dumps({"products": []}), "请至少添加一个产品"),
        (
            json.dumps({"products": [{"product_name": "丹参", "jar": 0, "herb_overall": 0, "herb_detail": 2}]}),
            "三类素材不能使用同一张图片",
        ),
        (
            json.dumps({"products": [{"product_name": "丹参", "jar": 0, "herb_overall": 1, "herb_detail": 9}]}),
            "图片索引无效",
        ),
        (
            json.dumps({"products": [{"product_name": "", "jar": 0, "herb_overall": 1, "herb_detail": 2}]}),
            "产品名称不能为空",
        ),
    ],
)
def test_rejects_invalid_manifest(client, raw_manifest, expected_detail):
    test_client, _ = client
    response = test_client.post(
        "/api/jobs",
        data={"manifest": raw_manifest, "templates": "single"},
        files=upload_files(3),
    )

    assert response.status_code == 400
    assert expected_detail in response.json()["detail"]


def test_rejects_unlisted_or_shared_uploads(client):
    test_client, _ = client
    unlisted = batch_manifest(("丹参",))
    response = create_batch(test_client, unlisted, upload_files(4), "single")
    assert response.status_code == 400
    assert response.json()["detail"] == "上传图片与清单不一致"

    shared = {
        "products": [
            {"product_name": "丹参", "jar": 0, "herb_overall": 1, "herb_detail": 2},
            {"product_name": "黄芪", "jar": 0, "herb_overall": 3, "herb_detail": 4},
        ]
    }
    response = create_batch(test_client, shared, upload_files(5), "single")
    assert response.status_code == 400
    assert response.json()["detail"] == "不同产品不能重复引用同一张上传图片"


def test_rejects_invalid_image_and_removes_partial_directory(client):
    test_client, output_dir = client
    files = upload_files(3)
    files[1] = ("uploads", ("broken.png", b"not-an-image", "image/png"))

    response = create_batch(test_client, batch_manifest(("丹参",)), files, "single")

    assert response.status_code == 400
    assert "无法识别为有效图片" in response.json()["detail"]
    assert not list(output_dir.glob("*"))


def test_continues_after_template_failure_and_marks_partial(client, monkeypatch):
    test_client, _ = client

    def partly_failing_generate(*, output_path, template_path, progress, **kwargs):
        progress("正在替换罐体", 1)
        if Path(output_path).parent.name == "p001" and Path(template_path).name == "2.jpg":
            raise RuntimeError("模拟模板失败")
        progress("正在替换盘中药材", 2)
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (12, 12), "blue").save(output_path)
        return {"jar_model": "mock-jar", "herb_model": "mock-herb"}

    monkeypatch.setattr(server, "generate_effect", partly_failing_generate)
    response = create_batch(test_client)
    job_id = response.json()["id"]
    job = test_client.get(f"/api/jobs/{job_id}").json()

    assert job["status"] == "partial"
    assert job["completed_steps"] == job["total_steps"]
    assert job["products"][0]["status"] == "partial"
    assert len(job["products"][0]["results"]) == 1
    assert job["products"][0]["errors"][0]["message"] == "模拟模板失败"
    assert job["products"][1]["status"] == "completed"
    assert len(job["products"][1]["results"]) == 2
    assert (server.OUTPUT_DIR / job_id / "inputs").is_dir()
    assert "_progress" not in job

    retried_templates = []

    def successful_retry(*, output_path, template_path, progress, **kwargs):
        retried_templates.append(Path(template_path).name)
        progress("正在替换罐体", 1)
        progress("正在替换盘中药材", 2)
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (12, 12), "blue").save(output_path)
        return {"jar_model": "mock-jar", "herb_model": "mock-herb"}

    monkeypatch.setattr(server, "generate_effect", successful_retry)
    retry_response = test_client.post(
        f"/api/jobs/{job_id}/retry",
        json={"items": [{"product_id": "p001", "template_id": "double"}]},
    )
    assert retry_response.status_code == 202

    retried_job = test_client.get(f"/api/jobs/{job_id}").json()
    assert retried_job["status"] == "completed"
    assert all(product["status"] == "completed" for product in retried_job["products"])
    assert all(len(product["results"]) == 2 for product in retried_job["products"])
    assert retried_templates == ["2.jpg"]
    assert not (server.OUTPUT_DIR / job_id / "inputs").exists()


def test_enforces_ten_product_batch_limit(client):
    test_client, _ = client
    accepted = create_batch(test_client, batch_manifest(tuple(f"产品{i}" for i in range(10))))
    assert accepted.status_code == 202

    rejected_manifest = batch_manifest(tuple(f"产品{i}" for i in range(11)))
    rejected = create_batch(
        test_client,
        rejected_manifest,
        upload_files(33),
    )
    assert rejected.status_code == 400
    assert rejected.json()["detail"] == "单批最多上传 10 个产品"


def test_limits_generation_concurrency_to_two(client, monkeypatch):
    test_client, _ = client
    state_lock = threading.Lock()
    active = 0
    peak = 0

    def tracked_generate(*, output_path, progress, **kwargs):
        nonlocal active, peak
        with state_lock:
            active += 1
            peak = max(peak, active)
        try:
            progress("正在替换罐体", 1)
            time.sleep(0.03)
            progress("正在替换盘中药材", 2)
            output_path = Path(output_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (12, 12), "blue").save(output_path)
            return {"jar_model": "mock-jar", "herb_model": "mock-herb"}
        finally:
            with state_lock:
                active -= 1

    monkeypatch.setattr(server, "generate_effect", tracked_generate)
    response = create_batch(test_client, batch_manifest(("产品一", "产品二", "产品三")))
    job = test_client.get(f"/api/jobs/{response.json()['id']}").json()

    assert job["status"] == "completed"
    assert job["completed_steps"] == job["total_steps"] == 12
    assert peak == 2


def test_retries_all_failed_templates(client, monkeypatch):
    test_client, _ = client

    def fail_double(*, output_path, template_path, progress, **kwargs):
        progress("正在替换罐体", 1)
        if Path(template_path).name == "2.jpg":
            raise RuntimeError("双罐模板暂时失败")
        progress("正在替换盘中药材", 2)
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (12, 12), "blue").save(output_path)
        return {"jar_model": "mock-jar", "herb_model": "mock-herb"}

    monkeypatch.setattr(server, "generate_effect", fail_double)
    response = create_batch(test_client)
    job_id = response.json()["id"]
    assert test_client.get(f"/api/jobs/{job_id}").json()["status"] == "partial"

    retried = []

    def successful_retry(*, output_path, template_path, progress, **kwargs):
        retried.append(Path(template_path).name)
        progress("正在替换罐体", 1)
        progress("正在替换盘中药材", 2)
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (12, 12), "blue").save(output_path)
        return {"jar_model": "mock-jar", "herb_model": "mock-herb"}

    monkeypatch.setattr(server, "generate_effect", successful_retry)
    retry_response = test_client.post(f"/api/jobs/{job_id}/retry", json={})
    assert retry_response.status_code == 202

    job = test_client.get(f"/api/jobs/{job_id}").json()
    assert job["status"] == "completed"
    assert retried == ["2.jpg", "2.jpg"]
    assert all(not product["errors"] for product in job["products"])


def test_rejects_retry_without_failures(client):
    test_client, _ = client
    response = create_batch(test_client, batch_manifest(("丹参",)), templates="single")
    retry = test_client.post(f"/api/jobs/{response.json()['id']}/retry", json={})

    assert retry.status_code == 409
    assert retry.json()["detail"] == "当前任务没有可重试的失败项"
