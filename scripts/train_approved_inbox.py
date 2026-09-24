"""Train one calibrated three-class model from owner-approved private inbox labels."""
import argparse
from collections import Counter
import gc
import json
import math
from pathlib import Path
import random
import time

from src.config import Settings
from src.email_text import MODEL_MAX_TOKENS, format_model_text
from src.evaluation import classification_metrics
from src.inbox_training import prepare_user_approved_splits
from src.prediction import ID2LABEL, LABEL2ID


def encode(tokenizer, rows, max_tokens):
    return tokenizer(
        [format_model_text(row["sender"], row["subject"], row["body"]) for row in rows],
        padding=True, truncation=True, max_length=max_tokens,
        return_tensors="pt", return_token_type_ids=False,
    )


def probability_rows(model, encoded, batch_size):
    import torch
    model.eval()
    output = []
    with torch.no_grad():
        for offset in range(0, len(encoded["input_ids"]), batch_size):
            batch = {key: value[offset:offset + batch_size] for key, value in encoded.items()}
            output.extend(model(**batch).logits.softmax(dim=-1).tolist())
    return output


def categories(probabilities, confidence=0.0, margin=0.0):
    output = []
    for row in probabilities:
        ordered = sorted(range(len(row)), key=row.__getitem__, reverse=True)
        first, second = ordered[:2]
        output.append(ID2LABEL[first] if row[first] >= confidence and row[first] - row[second] >= margin else None)
    return output


def score(metrics):
    important = metrics["per_class"]["IMPORTANT"]["recall"]
    updates = metrics["per_class"]["UPDATES"]["recall"]
    objective = metrics["macro_f1"] + 0.15 * important + 0.05 * updates
    return (
        objective,
        metrics["macro_f1"],
        important,
        updates,
        metrics["accuracy_including_abstention"],
    )


def select_thresholds(truth, probabilities):
    candidates = []
    for confidence in (0.0, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70):
        for margin in (0.0, 0.05, 0.10, 0.15, 0.20):
            metrics = classification_metrics(truth, categories(probabilities, confidence, margin))
            candidates.append((score(metrics) + (metrics["coverage"], -confidence, -margin),
                               confidence, margin, metrics))
    _, confidence, margin, metrics = max(candidates, key=lambda item:item[0])
    return confidence, margin, metrics


def set_trainable_layers(model, top_layers):
    for parameter in model.parameters():
        parameter.requires_grad = True
    for parameter in model.distilbert.parameters():
        parameter.requires_grad = False
    if top_layers:
        for layer in model.distilbert.transformer.layer[-top_layers:]:
            for parameter in layer.parameters():
                parameter.requires_grad = True


def train_epoch(model, loader, input_keys, loss_function, optimizer):
    import torch
    model.train()
    loss_sum = 0.0
    for values in loader:
        batch = {key: values[index] for index, key in enumerate(input_keys)}
        target = values[-1]
        optimizer.zero_grad(set_to_none=True)
        loss = loss_function(model(**batch).logits, target)
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
        optimizer.step()
        loss_sum += float(loss.detach())
    return loss_sum


def run(args):
    import numpy as np
    import torch
    from torch.utils.data import DataLoader, TensorDataset
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    output = Path(args.output).resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError("Choose a new or empty output directory; existing checkpoints are preserved")
    if args.confirm_approved_labels != "YES":
        raise ValueError("Mailbox-owner approval is required")

    settings = Settings.from_environment(load_file=True)
    splits, dataset = prepare_user_approved_splits(settings.db_path, args.split_seed)
    tokenizer = AutoTokenizer.from_pretrained(args.base_model, local_files_only=True, token=False)
    encoded = {name: encode(tokenizer, rows, args.max_tokens) for name, rows in splits.items()}
    input_keys = tuple(encoded["train"])
    train_labels = torch.tensor([LABEL2ID[row["human_label"]] for row in splits["train"]])
    train_data = TensorDataset(*(encoded["train"][key] for key in input_keys), train_labels)
    truth_validation = [row["human_label"] for row in splits["validation"]]
    truth_test = [row["human_label"] for row in splits["test"]]
    counts = Counter(row["human_label"] for row in splits["train"])
    weights = torch.tensor([
        math.sqrt(len(splits["train"]) / (len(LABEL2ID) * counts[ID2LABEL[index]]))
        for index in range(len(LABEL2ID))
    ], dtype=torch.float32)
    loss_function = torch.nn.CrossEntropyLoss(weight=weights, label_smoothing=args.label_smoothing)

    print(json.dumps({"dataset": dataset, "training_seeds": args.seeds}, indent=2), flush=True)
    started = time.perf_counter()
    selected_state = None
    selected_score = None
    selected_seed = None
    selected_validation = None
    runs = []
    final_model = None

    for seed in args.seeds:
        random.seed(seed)
        np.random.seed(seed)
        torch.manual_seed(seed)
        torch.set_num_threads(args.threads)
        model = AutoModelForSequenceClassification.from_pretrained(
            args.base_model, local_files_only=True, token=False, num_labels=3,
            id2label=ID2LABEL, label2id=LABEL2ID, ignore_mismatched_sizes=True,
        )
        loader = DataLoader(
            train_data, batch_size=args.batch_size, shuffle=True,
            generator=torch.Generator().manual_seed(seed),
        )
        history = []
        seed_state = None
        seed_score = None
        seed_validation = None

        stages = [
            ("head_warmup", 0, args.head_epochs, args.head_learning_rate),
            ("upper_finetune", args.unfreeze_layers, args.finetune_epochs, args.learning_rate),
        ]
        epoch_number = 0
        stale = 0
        for stage, layers, epochs, learning_rate in stages:
            set_trainable_layers(model, layers)
            optimizer = torch.optim.AdamW(
                (parameter for parameter in model.parameters() if parameter.requires_grad),
                lr=learning_rate, weight_decay=0.01,
            )
            for _ in range(epochs):
                epoch_number += 1
                loss_sum = train_epoch(model, loader, input_keys, loss_function, optimizer)
                probabilities = probability_rows(model, encoded["validation"], args.batch_size)
                metrics = classification_metrics(truth_validation, categories(probabilities))
                current_score = score(metrics)
                history.append({
                    "epoch": epoch_number, "stage": stage,
                    "train_loss_sum": loss_sum, "validation": metrics,
                })
                print(json.dumps({
                    "seed": seed, "epoch": epoch_number, "stage": stage,
                    "loss": round(loss_sum, 6),
                    "validation_macro_f1": round(metrics["macro_f1"], 6),
                    "validation_important_recall": round(metrics["per_class"]["IMPORTANT"]["recall"], 6),
                    "validation_updates_recall": round(metrics["per_class"]["UPDATES"]["recall"], 6),
                    "validation_spam_recall": round(metrics["per_class"]["SPAM"]["recall"], 6),
                }), flush=True)
                if seed_score is None or current_score > seed_score:
                    seed_score = current_score
                    seed_validation = metrics
                    seed_state = {name:value.detach().cpu().clone() for name,value in model.state_dict().items()}
                    stale = 0
                else:
                    stale += 1
                if stage == "upper_finetune" and stale >= args.patience:
                    break

        runs.append({"seed":seed, "history":history, "selected_validation":seed_validation})
        if selected_score is None or seed_score > selected_score:
            selected_score = seed_score
            selected_seed = seed
            selected_validation = seed_validation
            selected_state = seed_state
        final_model = model
        del optimizer, loader, seed_state
        gc.collect()

    final_model.load_state_dict(selected_state)
    validation_probabilities = probability_rows(final_model, encoded["validation"], args.batch_size)
    confidence, margin, calibrated_validation = select_thresholds(truth_validation, validation_probabilities)
    test_probabilities = probability_rows(final_model, encoded["test"], args.batch_size)
    raw_test = classification_metrics(truth_test, categories(test_probabilities))
    calibrated_test = classification_metrics(
        truth_test, categories(test_probabilities, confidence, margin)
    )
    elapsed = time.perf_counter() - started

    version = "inbox-approved-v2-" + dataset["dataset_hash"][:10] + "-" + str(selected_seed)
    final_model.config.mailmind_model_version = version
    final_model.config.mailmind_training_scope = "private_user_approved_inbox"
    final_model.config.mailmind_training_dataset_hash = dataset["dataset_hash"]
    final_model.config.mailmind_training_label_policy = dataset["label_policy"]
    final_model.config.mailmind_uses_sender = True
    final_model.config.mailmind_confidence_threshold = confidence
    final_model.config.mailmind_margin_threshold = margin
    final_model.config.mailmind_test_macro_f1 = calibrated_test["macro_f1"]
    final_model.config.mailmind_test_important_recall = calibrated_test["per_class"]["IMPORTANT"]["recall"]

    output.mkdir(parents=True, exist_ok=True)
    final_model.save_pretrained(output, safe_serialization=True)
    tokenizer.save_pretrained(output)
    report = {
        "version": version,
        "scope": "private_user_approved_inbox",
        "dataset": dataset,
        "base_model": args.base_model,
        "selected_seed": selected_seed,
        "hyperparameters": {
            "split_seed": args.split_seed, "seeds":args.seeds,
            "head_epochs":args.head_epochs, "finetune_epochs":args.finetune_epochs,
            "head_learning_rate":args.head_learning_rate,
            "learning_rate":args.learning_rate, "unfreeze_layers":args.unfreeze_layers,
            "batch_size":args.batch_size, "max_tokens":args.max_tokens,
            "label_smoothing":args.label_smoothing,
            "class_weights":{ID2LABEL[index]:float(value) for index,value in enumerate(weights)},
        },
        "selection": "validation macro F1 plus bounded IMPORTANT and UPDATES recall bonuses",
        "runs": runs,
        "selected_validation_raw": selected_validation,
        "confidence_policy": {
            "confidence_threshold":confidence, "top_two_margin_threshold":margin,
            "validation":calibrated_validation,
        },
        "test_raw":raw_test,
        "test_calibrated":calibrated_test,
        "training_seconds":elapsed,
        "limitations":[
            "Private single-mailbox data; not validated for other users.",
            "Most labels are owner-approved prior predictions, not individual corrections.",
            "Small held-out class counts make metrics uncertain.",
            "New data changes weights only after explicit retraining and evaluation.",
        ],
    }
    (output/"training_report.json").write_text(
        json.dumps(report,indent=2,allow_nan=False)+"\n",encoding="utf-8"
    )
    print(json.dumps({
        "saved":str(output), "version":version, "selected_seed":selected_seed,
        "confidence_policy":report["confidence_policy"], "test_raw":raw_test,
        "test_calibrated":calibrated_test, "training_seconds":elapsed,
    },indent=2),flush=True)
    return report


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",required=True)
    parser.add_argument("--base-model",default="distilbert-base-uncased")
    parser.add_argument("--confirm-approved-labels",required=True)
    parser.add_argument("--split-seed",type=int,default=42)
    parser.add_argument("--seeds",type=int,nargs="+",default=[13,42,73])
    parser.add_argument("--head-epochs",type=int,default=1)
    parser.add_argument("--finetune-epochs",type=int,default=4)
    parser.add_argument("--batch-size",type=int,default=8)
    parser.add_argument("--head-learning-rate",type=float,default=5e-4)
    parser.add_argument("--learning-rate",type=float,default=2e-5)
    parser.add_argument("--unfreeze-layers",type=int,choices=range(1,7),default=2)
    parser.add_argument("--label-smoothing",type=float,default=0.05)
    parser.add_argument("--patience",type=int,default=2)
    parser.add_argument("--max-tokens",type=int,default=MODEL_MAX_TOKENS)
    parser.add_argument("--threads",type=int,default=4)
    run(parser.parse_args())
