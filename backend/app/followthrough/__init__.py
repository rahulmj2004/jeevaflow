"""
AI Follow-Through Engine: open-vocabulary matching of documented
doctor instructions (Open Loops) to later source-quoted results, using
a local biomedical sentence-embedding model.

    encoder.py   pinned PubMedBERT embedding model, pure NumPy
                 inference (runs only in the isolated worker)
    text.py      what is embedded: the test concept of an
                 instruction, the label of a result line
    semantic.py  scoring, calibrated abstention and explanations
                 (vector maths only; runs in the API process)

The AI only ranks existing, quoted evidence. It never creates a
result, never closes a loop, and abstains below a calibrated
threshold; a doctor confirms every suggestion.
"""
