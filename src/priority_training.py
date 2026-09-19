"""Explicit offline CPU demo training; no import-time weights or provider calls."""
import json
import random
from pathlib import Path
from .email_text import format_email_text
from .prediction import LABEL2ID,ID2LABEL,checkpoint_labels
from .benchmark_data import digest
from .evaluation import classification_metrics


def validate_training_rows(rows,minimum_per_class=2):
    from collections import Counter
    counts=Counter(row['human_label'] for row in rows)
    if set(counts)!=set(LABEL2ID) or any(counts[label]<minimum_per_class for label in LABEL2ID):
        raise ValueError('Training and personalization need all three classes with sufficient examples')
    return counts


def train_tokenizer(rows):
    from tokenizers import Tokenizer, models, normalizers, pre_tokenizers
    from transformers import PreTrainedTokenizerFast
    from tokenizers.processors import TemplateProcessing
    # Trainer merge ties can change IDs across processes. Build a sorted,
    # training-only WordPiece vocabulary instead; unseen words use characters.
    normalization=normalizers.BertNormalizer(lowercase=True)
    pretokenizer=pre_tokenizers.BertPreTokenizer()
    words=set()
    for row in rows:
        text=normalization.normalize_str(format_email_text(row['subject'],row['body']))
        words.update(token for token,_ in pretokenizer.pre_tokenize_str(text))
    characters=set(''.join(words))
    special=['[PAD]','[UNK]','[CLS]','[SEP]','[MASK]']
    tokens=special+sorted((words|characters|{'##'+char for char in characters})-set(special))
    tokenizer=Tokenizer(models.WordPiece(vocab={token:index for index,token in enumerate(tokens)},unk_token='[UNK]'))
    tokenizer.normalizer=normalization;tokenizer.pre_tokenizer=pretokenizer
    tokenizer.post_processor=TemplateProcessing(single='[CLS] $A [SEP]',pair='[CLS] $A [SEP] $B:1 [SEP]:1',
        special_tokens=[('[CLS]',tokenizer.token_to_id('[CLS]')),('[SEP]',tokenizer.token_to_id('[SEP]'))])
    return PreTrainedTokenizerFast(tokenizer_object=tokenizer,unk_token='[UNK]',pad_token='[PAD]',
        cls_token='[CLS]',sep_token='[SEP]',mask_token='[MASK]',model_max_length=512,model_input_names=['input_ids','attention_mask'])



def encode(tokenizer,rows):
    return tokenizer([format_email_text(row['subject'],row['body']) for row in rows],padding=True,truncation=True,max_length=128,return_tensors='pt',return_token_type_ids=False)


def infer(model,tokenizer,rows):
    import time
    import torch
    start=time.perf_counter();model.eval()
    predictions=[];scores=[]
    with torch.no_grad():
        for offset in range(0,len(rows),16):
            output=model(**encode(tokenizer,rows[offset:offset+16])).logits.softmax(dim=-1)
            for probabilities in output:
                index=int(probabilities.argmax());predictions.append(ID2LABEL[index]);scores.append(float(probabilities[index]))
    return predictions,scores,(time.perf_counter()-start)*1000/max(1,len(rows))


def train(rows,validation,output,*,seed=42,epochs=20,base_path=None):
    output = Path(output)
    if output.exists() and any(output.iterdir()):
        raise ValueError('Training output must be empty; existing checkpoints are never overwritten')
    import numpy as np
    import torch
    from transformers import DistilBertConfig,DistilBertForSequenceClassification,AutoTokenizer
    validate_training_rows(rows);validate_training_rows(validation,1)
    if not 1<=epochs<=100: raise ValueError('Epoch count outside supported range')
    if len({row['group_id'] for row in rows}&{row['group_id'] for row in validation}): raise ValueError('Training/validation group leakage')
    random.seed(seed);np.random.seed(seed);torch.manual_seed(seed);torch.set_num_threads(2)
    torch.use_deterministic_algorithms(True)
    if base_path:
        config=DistilBertConfig.from_pretrained(str(base_path),local_files_only=True)
        checkpoint_labels(config)
        model=DistilBertForSequenceClassification.from_pretrained(str(base_path),local_files_only=True)
        tokenizer=AutoTokenizer.from_pretrained(str(base_path),local_files_only=True)
    else:
        tokenizer=train_tokenizer(rows)
        config=DistilBertConfig(vocab_size=len(tokenizer),max_position_embeddings=512,n_layers=2,n_heads=4,dim=96,
            hidden_dim=192,num_labels=3,id2label=ID2LABEL,label2id=LABEL2ID,dropout=.1,attention_dropout=.1,seq_classif_dropout=.1)
        model=DistilBertForSequenceClassification(config)
    role='personal' if base_path else 'base'
    version=f'synthetic-{role}-'+digest({'rows':rows,'seed':seed,'epochs':epochs,'architecture':'tiny-distilbert-v2'})[:12]
    model.config.mailmind_model_version=version
    model.config.mailmind_training_scope='synthetic_benchmark_only'
    model.config.mailmind_training_dataset_hash=digest(rows)
    optimizer=torch.optim.AdamW(model.parameters(),lr=.001 if not base_path else .0002,weight_decay=.01)
    labels=torch.tensor([LABEL2ID[row['human_label']] for row in rows]);inputs=encode(tokenizer,rows)
    best=-1;history=[];output=Path(output);output.mkdir(parents=True,exist_ok=True)
    generator=torch.Generator().manual_seed(seed)
    for epoch in range(epochs):
        model.train();loss_sum=0
        for indexes in torch.randperm(len(rows),generator=generator).split(16):
            optimizer.zero_grad(set_to_none=True)
            result=model(**{key:value[indexes] for key,value in inputs.items()},labels=labels[indexes]);result.loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(),1);optimizer.step();loss_sum+=float(result.loss.detach())
        guesses,_,_=infer(model,tokenizer,validation)
        metrics=classification_metrics([row['human_label'] for row in validation],guesses)
        history.append({'epoch':epoch+1,'train_loss_sum':loss_sum,'validation_macro_f1':metrics['macro_f1'],'validation_urgent_false_negatives':metrics['urgent_false_negatives']})
        value=metrics['macro_f1']-.1*metrics['urgent_false_negatives']/len(validation)
        if value>best:
            best=value;model.save_pretrained(output,safe_serialization=True);tokenizer.save_pretrained(output)
    model=DistilBertForSequenceClassification.from_pretrained(output,local_files_only=True)
    (output/'training_history.json').write_text(json.dumps({'version':version,'seed':seed,'selection_split':'validation','history':history},indent=2)+'\n',encoding='utf-8')
    return model,tokenizer,history


def load_training_frame(dataset_path='fixtures/priority_benchmark',stage='base'):
    import pandas as pd
    from .benchmark_data import prepare_rows,group_splits,personalization_rows
    root=Path(dataset_path)
    manifest=json.loads((root/'provenance.json').read_text(encoding='utf-8'))
    rows=prepare_rows(json.loads((root/'emails.json').read_text(encoding='utf-8')),manifest)
    splits=group_splits(rows)
    selected=personalization_rows(splits['train']) if stage=='personal' else splits['train']
    validate_training_rows(selected)
    return pd.DataFrame(selected)[['subject','body','human_label']]
