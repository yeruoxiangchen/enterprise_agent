#!/usr/bin/env bash
set -euo pipefail
OCR_ROOT=/data/zjr/enterprise_kb_agent
export PYTHONDONTWRITEBYTECODE=1 CUDA_VISIBLE_DEVICES=""
export TMPDIR="$OCR_ROOT/tmp" TORCH_HOME="$OCR_ROOT/cache/torch"
export HF_HOME="$OCR_ROOT/cache/huggingface" HF_HUB_CACHE="$OCR_ROOT/cache/huggingface/hub"
export TORCH_EXTENSIONS_DIR="$OCR_ROOT/cache/torch_extensions" MPLCONFIGDIR="$OCR_ROOT/cache/matplotlib"
export PIP_CACHE_DIR="$OCR_ROOT/cache/pip" XDG_CACHE_HOME="$OCR_ROOT/cache"
export OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1
umask 077
mkdir -p "$TMPDIR"
OCR_PACKAGES="$OCR_ROOT/tools/rapidocr-1.4.4"
if [[ ! -f "$OCR_PACKAGES/rapidocr_onnxruntime/__init__.py" ]]; then
  "$OCR_ROOT/venv-agentpack/bin/python" -B -m pip install --disable-pip-version-check --no-compile \
    --index-url "${KB_PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}" --timeout 90 \
    --target "$OCR_PACKAGES" rapidocr_onnxruntime==1.4.4 onnxruntime==1.20.1 \
    opencv-python==4.10.0.84 numpy==2.2.6
fi
"$OCR_ROOT/venv-agentpack/bin/python" -B - "$OCR_PACKAGES" <<'PY'
import hashlib
import importlib.metadata
import json
from pathlib import Path
import sys
root=Path(sys.argv[1]);sys.path.insert(0,str(root))
from rapidocr_onnxruntime import RapidOCR
import onnxruntime
assert onnxruntime.get_device()=='CPU'
assert importlib.metadata.version('rapidocr_onnxruntime')=='1.4.4'
assert onnxruntime.__version__=='1.20.1'
engine=RapidOCR(intra_op_num_threads=2,inter_op_num_threads=1,
                det_use_cuda=False,cls_use_cuda=False,rec_use_cuda=False)
models={p.name:hashlib.sha256(p.read_bytes()).hexdigest()for p in (root/'rapidocr_onnxruntime/models').glob('*.onnx')}
assert len(models)>=3,'Packaged detector, classifier and recognizer must all exist'
versions={d.metadata['Name']:d.version for d in importlib.metadata.distributions(path=[str(root)])}
record={'version':1,'backend':'rapidocr_v4_cpu','runtime_downloads':False,
        'dependencies':versions,'onnx_sha256':models,'providers':onnxruntime.get_available_providers()}
temporary=root/'READY.tmp';temporary.write_text(json.dumps(record,indent=2)+'\n');temporary.replace(root/'READY.json')
print(json.dumps({'backend':record['backend'],'models':len(models),'packages':str(root)}))
PY
