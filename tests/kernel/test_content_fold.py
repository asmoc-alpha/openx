"""agent 附件归一化单测：``openx_file`` part 发 provider 前折成文本指引。

serve 上传的非图片文件以自定义 part（``openx_file``）挂在用户消息 content
里供 web 展示；provider 不认识该 part（openai 透传 / anthropic 只收
text+image_url）→ 发请求前必须在 run/stream_run 折成文本。本测锁定纯函数
``_fold_openx_files`` 的不变量：不改动入参、图片与文本原位保留、折出指引。
"""

from __future__ import annotations

from openx.agent import _fold_openx_files


def _file_part(name: str = "a.py", rel: str = ".openx/uploads/s/a.py") -> dict:
    return {"type": "openx_file", "name": name, "size": 3,
            "mime": "text/x-python", "relPath": rel}


def test_str_content_passthrough():
    assert _fold_openx_files("plain") == "plain"


def test_no_file_parts_returns_same_content():
    content = [{"type": "text", "text": "hi"},
               {"type": "image_url", "image_url": {"url": "data:image/png;base64,QQ=="}}]
    assert _fold_openx_files(content) is content      # 无 openx_file → 原样


def test_folds_file_part_into_text_and_keeps_image():
    content = [
        {"type": "text", "text": "看这个文件"},
        _file_part("a.py", ".openx/uploads/s/x/a.py"),
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,QQ=="}},
    ]
    original_text = content[0]["text"]
    out = _fold_openx_files(content)
    assert out is not content                          # 返回新列表，绝不动原历史
    # 原 content 未被改动
    assert content[0]["text"] == original_text
    assert content[1]["type"] == "openx_file"

    assert not any(p.get("type") == "openx_file" for p in out)
    types = [p.get("type") for p in out]
    assert types == ["text", "image_url"], types
    # 指引并进 text part，image 原位保留
    assert "a.py" in out[0]["text"]
    assert ".openx/uploads/s/x/a.py" in out[0]["text"]
    assert out[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_file_only_no_text_gets_leading_text():
    out = _fold_openx_files([_file_part("b.csv")])
    assert out[0]["type"] == "text" and "b.csv" in out[0]["text"]
    assert len(out) == 1
