from src.email_text import MODEL_MAX_TOKENS
from src.training_data import prepare_training_frame
from src.priority_training import load_training_frame
import pandas as pd
from datasets import Dataset
from transformers import AutoTokenizer

def prepare_data():
    print("1. Loading verified priority training groups...")
    df = load_training_frame()
    df = prepare_training_frame(df)
    hf_dataset = Dataset.from_pandas(df)

    print("4. Tokenizing...")
    tokenizer = AutoTokenizer.from_pretrained("distilbert-base-uncased", token=False)

    def tokenize_function(examples):
        # We truncate to 512 tokens (DistilBERT's max brain size)
        # We pad to max_length so all arrays are exactly the same size
        return tokenizer(examples["combined_text"], padding="max_length", truncation=True, max_length=MODEL_MAX_TOKENS)

    # Apply the tokenizer to all rows simultaneously (batched=True makes it lightning fast)
    tokenized_dataset = hf_dataset.map(tokenize_function, batched=True)
    
    print("\n--- DATASET READY ---")
    print(tokenized_dataset)
    return tokenized_dataset

if __name__ == "__main__":
    import argparse
    import json
    from pathlib import Path
    from src.benchmark_data import prepare_rows,group_splits,split_manifest,digest
    parser=argparse.ArgumentParser(description='Validate provenance and reserve grouped splits without training or loading weights')
    parser.add_argument('--dataset',default='fixtures/priority_benchmark')
    parser.add_argument('--output',required=True)
    parser.add_argument('--seed',type=int,default=42)
    args=parser.parse_args();root=Path(args.dataset)
    manifest=json.loads((root/'provenance.json').read_text(encoding='utf-8'))
    rows=prepare_rows(json.loads((root/'emails.json').read_text(encoding='utf-8')),manifest)
    splits=group_splits(rows,args.seed);output=Path(args.output);output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(split_manifest(splits,digest(rows),args.seed),indent=2)+'\n',encoding='utf-8')
    print('Validated three-category grouped splits. Test IDs are reserved separately.')
