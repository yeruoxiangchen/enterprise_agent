"""Bounded CPU OCR subprocess; packaged ONNX weights, no runtime downloads."""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import json
import math
import os
from pathlib import Path
import sys


def source_box(polygon,width,height,padding):
    x=[float(point[0])-padding for point in polygon]
    y=[float(point[1])-padding for point in polygon]
    box=[max(0,math.floor(min(x))),max(0,math.floor(min(y))),
         min(width,math.ceil(max(x))),min(height,math.ceil(max(y)))]
    return box if box[2]>box[0]and box[3]>box[1]else None


def reading_order(lines):
    """Keep vertically adjacent lines in their column before visiting logos.

    Comparing against the most recent line prevents a wide page header from
    joining unrelated columns. All decisions use detected geometry only.
    """
    blocks=[]
    for line in sorted(lines,key=lambda row:(row['box'][1],row['box'][0])):
        left,top,right,bottom=line['box'];height=bottom-top
        candidates=[]
        for index,block in enumerate(blocks):
            a,b,c,d=block[-1]['box'];previous_height=d-b
            overlap=max(0,min(right,c)-max(left,a))
            width=max(1,min(right-left,c-a))
            gap=top-d
            aligned=min(abs(left-a),abs(right-c)) <= max(height,previous_height)*1.5
            if (top >= b+previous_height*.5 and -previous_height*.25 <= gap <= max(height,previous_height)*1.8
                    and overlap/width >= .5 and aligned):
                candidates.append((max(0,gap),min(abs(left-a),abs(right-c)),index))
        if candidates:
            blocks[min(candidates)[2]].append(line)
        else:
            blocks.append([line])
    blocks.sort(key=lambda block:(block[0]['box'][1],block[0]['box'][0]))
    result=[]
    for block_id,block in enumerate(blocks):
        for line in block:
            result.append({**line,'block':block_id,'number':len(result)+1})
    return result


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--image',type=Path,required=True)
    parser.add_argument('--packages',type=Path,required=True)
    args=parser.parse_args()
    os.environ.update(CUDA_VISIBLE_DEVICES='',OMP_NUM_THREADS='2',OMP_THREAD_LIMIT='2',
                      OPENBLAS_NUM_THREADS='1',MKL_NUM_THREADS='1')
    sys.path.insert(0,str(args.packages))
    with redirect_stdout(sys.stderr):
        from rapidocr_onnxruntime import RapidOCR
        from PIL import Image
        import cv2
        engine=RapidOCR(intra_op_num_threads=2,inter_op_num_threads=1,
                        det_use_cuda=False,cls_use_cuda=False,rec_use_cuda=False,
                        det_limit_side_len=1280,det_limit_type='max')
        # A screenshot can clip a caption against its edge. Detector padding
        # keeps that edge away from the network boundary; coordinates below
        # are mapped back to the uploaded image, with padding removed.
        padding=32
        source=cv2.imread(str(args.image),cv2.IMREAD_COLOR)
        raster=cv2.copyMakeBorder(source,padding,padding,padding,padding,cv2.BORDER_REPLICATE)
        result,_=engine(raster)
    with Image.open(args.image)as image:width,height=image.size
    lines=[]
    for polygon,text,score in result or []:
        if not text.strip()or float(score)<.5:
            continue
        box=source_box(polygon,width,height,padding)
        if box is None:
            continue
        lines.append(dict(text=text,box=box,confidence=round(float(score)*100,2),ocr_pass='rapidocr_v4_cpu'))
    print(json.dumps(reading_order(lines),ensure_ascii=False),flush=True)


if __name__=='__main__':
    main()
