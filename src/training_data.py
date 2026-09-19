"""Strict three-category preparation: no guessed labels or silently dropped rows."""
from .email_text import prepare_training_text
from .prediction import LABEL2ID

def prepare_training_frame(frame):
    if not {'subject','body','human_label'}.issubset(frame.columns) or frame.empty:
        raise ValueError('Dataset must have rows and subject, body, human_label columns')
    frame = frame.copy()
    if not frame['human_label'].isin(LABEL2ID).all():
        raise ValueError('Unsupported labels. Use IMPORTANT, UPDATES, SPAM; review legacy IGNORE labels manually')
    frame['subject'] = frame['subject'].fillna('')
    frame['body'] = frame['body'].fillna('')
    if not frame[['subject','body']].map(lambda value: isinstance(value, str)).all().all():
        raise ValueError('Email text must be strings')
    if ((frame['subject'].str.strip() == '') & (frame['body'].str.strip() == '')).any():
        raise ValueError('Dataset contains empty emails; fix them instead of silently dropping them')
    frame = prepare_training_text(frame)
    frame['label'] = frame['human_label'].map(LABEL2ID).astype(int)
    return frame[['combined_text','label']]
