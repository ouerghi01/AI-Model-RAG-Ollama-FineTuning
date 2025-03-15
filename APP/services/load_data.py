
from langchain_core.documents import Document
import uuid
import os
import threading
from langchain_community.document_loaders import PDFPlumberLoader  
import pandas as pd 
class DataLoader:
    def __init__(self, upload_dir):
        self.UPLOAD_DIR = upload_dir
        os.makedirs(self.UPLOAD_DIR, exist_ok=True)

    def load_documents(self):
        docs = []
        all_files = os.listdir(self.UPLOAD_DIR)
        threads = []
        for file in all_files:
            if file.endswith(".pdf"):
                thread = threading.Thread(target=self.process_pdf_file, args=(docs, file))
                threads.append(thread)
            elif file.endswith(".csv"):
                thread = threading.Thread(target=self.load_csv_to_documents, args=(docs, file))
                threads.append(thread)

        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        doc_ids = [str(uuid.uuid4()) for _ in docs]
        return docs, doc_ids

    

    def process_pdf_file(self, docs, file):
        full_path = os.path.join(self.UPLOAD_DIR, file)
        if file.endswith("data.pdf") or not os.path.isfile(full_path):
            return

        loader = PDFPlumberLoader(full_path)
        document = loader.load()
        if document is not None:
            docs.extend(document)

    def load_csv_to_documents(self, docs, file):
        file_path = os.path.join(self.UPLOAD_DIR, file)
        if not file.endswith(".csv"):
            return

        df = pd.read_csv(file_path)
        df['meta'] = df.apply(lambda row: {"source": file_path}, axis=1)
        records = df.to_dict('records')
        columns = df.columns
        ds = []

        for record in records:
            full_text = "\n".join([f"{col}: {record[col]}" for col in columns])
            meta = record.get('meta', {})
            doc = Document(page_content=full_text, metadata=meta)
            ds.append(doc)

        docs.extend(ds)
