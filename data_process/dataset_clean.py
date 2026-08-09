# -*- coding: utf-8 -*-
import os
import json
from pathlib import Path
from datasets import load_dataset

# ===================== 修复Windows HF缓存报错环境变量 =====================
# 关闭软链接警告（不用开启Windows开发者模式）
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
# 禁用软链接，彻底规避权限问题
os.environ["HF_HUB_DISABLE_SYMLINKS"] = "1"
# HF国内镜像加速
os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
# 缓存放到项目内，不占用C盘用户目录
os.environ["HF_DATASETS_CACHE"] = str(Path("./datasets/hf_cache").resolve())
# 屏蔽未登录token限速警告
os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "0"
os.environ["HF_HUB_DISABLE_UNAUTHENTICATED_WARNING"] = "1"

# 项目数据集根目录
DATA_ROOT = Path("./datasets")
RAW_SCIQ = DATA_ROOT / "sciq_raw"
RAW_GSM8K = DATA_ROOT / "gsm8k_raw"
RAW_MMLU = DATA_ROOT / "mmlu_raw"
CLEAN_ROOT = DATA_ROOT / "cleaned"
CLEAN_SCIQ = CLEAN_ROOT / "sciq"
CLEAN_GSM8K = CLEAN_ROOT / "gsm8k"
CLEAN_MMLU = CLEAN_ROOT / "mmlu"

# 自动创建文件夹
for p in [RAW_SCIQ, RAW_GSM8K, RAW_MMLU, CLEAN_SCIQ, CLEAN_GSM8K, CLEAN_MMLU]:
    p.mkdir(exist_ok=True, parents=True)


def download_and_export_sciq():
    """下载SciQ 科学问答数据集（修复完整仓库名 allenai/sciq）"""
    print("===== 开始下载 SciQ =====")
    # 修复：补充命名空间 allenai/
    ds = load_dataset("allenai/sciq")
    splits = ["train", "validation", "test"]
    for split in splits:
        save_path = RAW_SCIQ / f"{split}.jsonl"
        with open(save_path, "w", encoding="utf-8") as f:
            for item in ds[split]:
                json.dump(item, f, ensure_ascii=False)
                f.write("\n")
        print(f"SciQ {split} 原始文件保存至：{save_path}")
    print("===== SciQ 下载完成 =====")


def download_and_export_gsm8k():
    """下载GSM8K数学应用题数据集（main子集）"""
    print("===== 开始下载 GSM8K =====")
    ds = load_dataset("openai/gsm8k", "main")
    splits = ["train", "test"]
    for split in splits:
        save_path = RAW_GSM8K / f"{split}.jsonl"
        with open(save_path, "w", encoding="utf-8") as f:
            for item in ds[split]:
                json.dump(item, f, ensure_ascii=False)
                f.write("\n")
        print(f"GSM8K {split} 原始文件保存至：{save_path}")
    print("===== GSM8K 下载完成 =====")


def download_and_export_mmlu():
    """下载MMLU多学科通用考试数据集"""
    print("===== 开始下载 MMLU =====")
    ds = load_dataset("cais/mmlu", "all")
    splits = ["dev", "test", "validation"]
    for split in splits:
        save_path = RAW_MMLU / f"{split}.jsonl"
        with open(save_path, "w", encoding="utf-8") as f:
            for item in ds[split]:
                json.dump(item, f, ensure_ascii=False)
                f.write("\n")
        print(f"MMLU {split} 原始文件保存至：{save_path}")
    print("===== MMLU 下载完成 =====")


def parse_gsm8k_answer(answer_text):
    """解析GSM8K的answer字段，拆分推理过程和最终答案"""
    reasoning_steps = []
    final_answer = None
    
    if "####" in answer_text:
        parts = answer_text.split("####")
        reasoning_part = parts[0].strip()
        answer_part = parts[1].strip() if len(parts) > 1 else ""
        
        for line in reasoning_part.split("\n"):
            line = line.strip()
            if line and not line.startswith("<<"):
                reasoning_steps.append(line)
        
        if answer_part:
            final_answer = ''.join(filter(lambda c: c.isdigit() or c == '.', answer_part))
            if final_answer:
                try:
                    if '.' in final_answer:
                        final_answer = float(final_answer)
                    else:
                        final_answer = int(final_answer)
                except ValueError:
                    final_answer = answer_part.strip()
    
    return reasoning_steps, final_answer


def clean_gsm8k():
    """清洗GSM8K数据集，拆分字段、过滤脏数据"""
    print("===== 开始清洗 GSM8K =====")
    splits = ["train", "test"]
    total_cleaned = 0
    total_filtered = 0
    
    for split in splits:
        input_path = RAW_GSM8K / f"{split}.jsonl"
        output_path = CLEAN_GSM8K / f"{split}.jsonl"
        
        if not input_path.exists():
            print(f"警告：{input_path} 不存在，跳过")
            continue
        
        cleaned_items = []
        with open(input_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                    question = item.get("question", "").strip()
                    answer = item.get("answer", "").strip()
                    
                    if not question or not answer:
                        total_filtered += 1
                        continue
                    
                    reasoning_steps, final_answer = parse_gsm8k_answer(answer)
                    
                    if final_answer is None:
                        total_filtered += 1
                        continue
                    
                    cleaned_item = {
                        "id": f"gsm8k_{split}_{len(cleaned_items):06d}",
                        "question": question,
                        "reasoning_steps": reasoning_steps,
                        "final_answer": final_answer,
                        "source": "gsm8k",
                        "split": split,
                        "type": "math_word_problem"
                    }
                    cleaned_items.append(cleaned_item)
                except Exception as e:
                    total_filtered += 1
                    continue
        
        with open(output_path, "w", encoding="utf-8") as f:
            for item in cleaned_items:
                json.dump(item, f, ensure_ascii=False)
                f.write("\n")
        
        total_cleaned += len(cleaned_items)
        print(f"GSM8K {split}: 清洗 {len(cleaned_items)} 条，过滤 {total_filtered} 条")
        print(f"    输出文件：{output_path}")
    
    print(f"===== GSM8K 清洗完成，共清洗 {total_cleaned} 条 =====")
    return total_cleaned


def clean_sciq():
    """清洗SciQ数据集，统一字段标准化、去除无效样本"""
    print("===== 开始清洗 SciQ =====")
    splits = ["train", "validation", "test"]
    total_cleaned = 0
    total_filtered = 0
    
    for split in splits:
        input_path = RAW_SCIQ / f"{split}.jsonl"
        output_path = CLEAN_SCIQ / f"{split}.jsonl"
        
        if not input_path.exists():
            print(f"警告：{input_path} 不存在，跳过")
            continue
        
        cleaned_items = []
        seen_questions = set()
        
        with open(input_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                    question = item.get("question", "").strip()
                    correct_answer = item.get("correct_answer", "").strip()
                    support = item.get("support", "").strip()
                    
                    if not question or not correct_answer:
                        total_filtered += 1
                        continue
                    
                    if question in seen_questions:
                        total_filtered += 1
                        continue
                    seen_questions.add(question)
                    
                    distractors = []
                    for i in range(1, 4):
                        distractor = item.get(f"distractor{i}", "").strip()
                        if distractor:
                            distractors.append(distractor)
                    
                    cleaned_item = {
                        "id": f"sciq_{split}_{len(cleaned_items):06d}",
                        "question": question,
                        "correct_answer": correct_answer,
                        "distractors": distractors,
                        "support": support,
                        "source": "sciq",
                        "split": split,
                        "type": "multiple_choice",
                        "domain": "science"
                    }
                    cleaned_items.append(cleaned_item)
                except Exception as e:
                    total_filtered += 1
                    continue
        
        with open(output_path, "w", encoding="utf-8") as f:
            for item in cleaned_items:
                json.dump(item, f, ensure_ascii=False)
                f.write("\n")
        
        total_cleaned += len(cleaned_items)
        print(f"SciQ {split}: 清洗 {len(cleaned_items)} 条，过滤 {total_filtered} 条")
        print(f"    输出文件：{output_path}")
    
    print(f"===== SciQ 清洗完成，共清洗 {total_cleaned} 条 =====")
    return total_cleaned


def clean_mmlu():
    """清洗MMLU数据集，统一字段标准化、去除无效样本"""
    print("===== 开始清洗 MMLU =====")
    splits = ["dev", "test", "validation"]
    total_cleaned = 0
    total_filtered = 0
    
    for split in splits:
        input_path = RAW_MMLU / f"{split}.jsonl"
        output_path = CLEAN_MMLU / f"{split}.jsonl"
        
        if not input_path.exists():
            print(f"警告：{input_path} 不存在，跳过")
            continue
        
        cleaned_items = []
        seen_questions = set()
        
        with open(input_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                    question = item.get("question", "").strip()
                    answer_idx = item.get("answer", "")
                    choices = item.get("choices", [])
                    
                    if not question:
                        total_filtered += 1
                        continue
                    
                    if question in seen_questions:
                        total_filtered += 1
                        continue
                    seen_questions.add(question)
                    
                    if isinstance(answer_idx, int) and 0 <= answer_idx < len(choices):
                        correct_answer = choices[answer_idx]
                    elif isinstance(answer_idx, str):
                        correct_answer = answer_idx
                    else:
                        correct_answer = ""
                    
                    cleaned_item = {
                        "id": f"mmlu_{split}_{len(cleaned_items):06d}",
                        "question": question,
                        "choices": choices,
                        "answer_idx": answer_idx,
                        "correct_answer": correct_answer,
                        "source": "mmlu",
                        "split": split,
                        "type": "multiple_choice",
                        "domain": "general_knowledge"
                    }
                    cleaned_items.append(cleaned_item)
                except Exception as e:
                    total_filtered += 1
                    continue
        
        with open(output_path, "w", encoding="utf-8") as f:
            for item in cleaned_items:
                json.dump(item, f, ensure_ascii=False)
                f.write("\n")
        
        total_cleaned += len(cleaned_items)
        print(f"MMLU {split}: 清洗 {len(cleaned_items)} 条，过滤 {total_filtered} 条")
        print(f"    输出文件：{output_path}")
    
    print(f"===== MMLU 清洗完成，共清洗 {total_cleaned} 条 =====")
    return total_cleaned


def full_clean_all_datasets():
    """一键清洗所有数据集"""
    clean_gsm8k()
    clean_sciq()
    clean_mmlu()
    print("\n✅ 全部数据集清洗完成！")
    print(f"清洗后数据存放目录：{CLEAN_ROOT.resolve()}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="数据集下载与清洗脚本")
    parser.add_argument("--download", action="store_true", help="下载原始数据集")
    parser.add_argument("--clean", action="store_true", help="清洗数据集")
    parser.add_argument("--all", action="store_true", help="下载并清洗所有数据集")
    
    args = parser.parse_args()
    
    if args.all or args.download:
        full_download_all_datasets()
    
    if args.all or args.clean:
        full_clean_all_datasets()