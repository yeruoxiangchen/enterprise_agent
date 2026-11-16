# 截图识别修复：2026-11-14

同8张公开网页截图、同公开答案、同评测脚本：关键文字完整恢复从4/8提升至8/8。企业8个样例的26个字段、知识文档匹配和工单流转保持全部通过；缺失字段继续由人工核对补充。

实现为CPU RapidOCR/ONNX Runtime，增加文字区域检测、分栏阅读顺序与通用边缘留白，保留原图坐标、OCR原文及置信度。Tesseract后端可继续显式选择。17项定向检查通过，两组16个最终样例的坐标和引用范围复核通过。

这批公开样例用于修复开发，指标为归一化后的关键字符串恢复；不作为全页逐字准确率或官方OCRBench总分。公开截图API耗时p50 4.06秒、p95 6.71秒，旧记录为1.71/3.22秒。新实现每次独立进程加载CPU模型，未占GPU。

修复在`/data/zjr/enterprise_kb_agent/artifacts/ocr_repair_20261114/worktree/`完成，补丁和前后文件摘要已保存。完整ITSM队列结束后，部署进程已自动应用6项源码改动并重启原8768服务，原账号和数据库沿用原路径。应用PID3915635，健康接口返回ok；主目录6个文件与验收候选摘要完全一致。查看应用记录：

```bash
cat /data/zjr/enterprise_kb_agent/artifacts/ocr_repair_20261114/rollout-status.json
```

[完整记录](/data/zjr/enterprise_kb_agent/artifacts/ocr_repair_20261114/report.md)、[验收摘要](/data/zjr/enterprise_kb_agent/artifacts/ocr_repair_20261114/validation.json)、[代码补丁](/data/zjr/enterprise_kb_agent/artifacts/ocr_repair_20261114/ocr_fix.patch)。

[八张公开图的前后识别与检测框对照](/data/zjr/enterprise_kb_agent/artifacts/ocr_repair_20261114/comparison.html)。

安装入口：`bash scripts/setup_rapidocr.sh`。依赖及模型位于项目`/data`目录，独立于共享Python环境；默认`auto`选择已安装的区域识别后端，可用`KB_INCIDENT_OCR_BACKEND=tesseract`选择原后端。
