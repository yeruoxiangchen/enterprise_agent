import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from kb.config import Settings
from kb.incident_ocr_worker import reading_order,source_box
from kb.incidents import image_lines


def test_reading_order_keeps_a_paragraph_together_beside_a_logo():
    lines=[
        dict(text='First line',box=[100,100,300,120]),
        dict(text='Brand',box=[340,126,400,146]),
        dict(text='Second line',box=[100,128,300,148]),
        dict(text='Last line',box=[200,156,300,176]),
    ]
    result=reading_order(lines)
    assert [row['text']for row in result]==['First line','Second line','Last line','Brand']
    assert [row['number']for row in result]==[1,2,3,4]
    assert result[0]['block']==result[2]['block']!=result[3]['block']
    assert {tuple(row['box'])for row in result}=={tuple(row['box'])for row in lines}


def test_detector_padding_maps_back_to_original_image():
    assert source_box([[42,52],[132,52],[132,92],[42,92]],100,80,32)==[10,20,100,60]
    assert source_box([[25,30],[135,30],[135,115],[25,115]],100,80,32)==[0,0,100,80]
    assert source_box([[0,0],[31,0],[31,31],[0,31]],100,80,32)is None


def test_rapid_worker_is_cpu_bounded_and_retains_redacted_source(monkeypatch,tmp_path):
    cfg=Settings(root=tmp_path,incident_ocr_backend='rapidocr',incident_ocr_packages=str(tmp_path/'packages'))
    packages=Path(cfg.incident_ocr_packages);packages.mkdir();(packages/'READY.json').write_text('{}')
    def worker(command,**kwargs):
        assert kwargs['env']['CUDA_VISIBLE_DEVICES']==''
        assert kwargs['timeout']==20
        return SimpleNamespace(returncode=0,stdout=json.dumps([dict(number=1,text='api_key=private-value',
            box=[10,10,200,30],confidence=99,ocr_pass='rapidocr_v4_cpu')]))
    monkeypatch.setattr(subprocess,'run',worker)
    result=image_lines(tmp_path/'image.png',cfg)
    assert result[0]['text']=='api_key=[REDACTED]'
    assert result[0]['box']==[10,10,200,30]


def test_rapid_timeout_returns_a_user_action(monkeypatch,tmp_path):
    cfg=Settings(root=tmp_path,incident_ocr_backend='rapidocr',incident_ocr_packages=str(tmp_path/'packages'))
    packages=Path(cfg.incident_ocr_packages);packages.mkdir();(packages/'READY.json').write_text('{}')
    def timeout(command,**kwargs):
        raise subprocess.TimeoutExpired(command,kwargs['timeout'])
    monkeypatch.setattr(subprocess,'run',timeout)
    with pytest.raises(ValueError,match='识别超时.*裁剪'):
        image_lines(tmp_path/'image.png',cfg)
