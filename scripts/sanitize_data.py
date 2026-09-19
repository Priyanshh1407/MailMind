"""Explicit, account-scoped minimal export; never run against a DB on import."""
from pathlib import Path
import argparse
import pandas as pd
from src.database import connection
from src.privacy import redact
from src.config import CATEGORIES, LEGACY_ACCOUNT


def mask_financial_pii(text):
    from src.privacy import redact
    return redact(text)


def process_and_save(*,db_path,account_id,output):
    if not account_id or account_id == LEGACY_ACCOUNT:
        raise ValueError('Choose an assigned account explicitly')
    output=Path(output)
    if output.exists(): raise ValueError('Export output already exists; choose a fresh path')
    with connection(db_path) as conn:
        rows=conn.execute('SELECT subject,body,human_label FROM email_logs WHERE account_id=? AND human_label IN (?,?,?)', (account_id,*CATEGORIES)).fetchall()
    def safe_cell(value):
        value=redact(value)
        return "'"+value if value.lstrip().startswith(('=','+','-','@')) else value
    frame=pd.DataFrame([{'subject':safe_cell(row['subject']),'body':safe_cell(row['body']),'human_label':row['human_label']} for row in rows],columns=['subject','body','human_label'])
    output.parent.mkdir(parents=True,exist_ok=True)
    with output.open('x',encoding='utf-8',newline='') as stream: frame.to_csv(stream,index=False)
    return len(frame)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',required=True);parser.add_argument('--account',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args();count=process_and_save(db_path=args.db,account_id=args.account,output=args.output)
    print(f'Exported {count} labelled rows. Masking is not guaranteed anonymization; inspect before sharing.')
