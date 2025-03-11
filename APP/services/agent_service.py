from langchain_community.document_loaders import CassandraLoader
from langchain.retrievers import ContextualCompressionRetriever
import logging
import time
from collections import OrderedDict
from langchain.retrievers.document_compressors import FlashrankRerank
from langchain.callbacks.manager import CallbackManager
from langchain.callbacks.streaming_stdout import StreamingStdOutCallbackHandler
from langchain_google_genai import (
    ChatGoogleGenerativeAI,
    HarmBlockThreshold,
    HarmCategory,
)
from fastapi.responses import HTMLResponse
import uuid
from langchain.retrievers.multi_query import MultiQueryRetriever

from langchain.storage import InMemoryStore
from langchain.text_splitter import TokenTextSplitter
from langchain.retrievers import ParentDocumentRetriever
from langchain_astradb import AstraDBVectorStore
from langchain_core.documents import Document
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import PromptTemplate
from langchain_core.runnables import RunnablePassthrough

from langchain_core.documents import Document
from langchain.memory import ConversationSummaryMemory
from dotenv import load_dotenv
import os 
from pathlib import Path
from langchain.retrievers import BM25Retriever, EnsembleRetriever


from langchain_community.document_loaders import (
    unstructured,
    
)


from services.pdf_service import PDFService
from langchain_core.prompts import ChatPromptTemplate
load_dotenv()  # take environment variables from .env.
os.environ["UPSTAGE_API_KEY"] = os.getenv("UPSTAGE_API_KEY")
from langchain_community.document_loaders import PDFPlumberLoader  
from langchain_community.embeddings import HuggingFaceEmbeddings  
from langchain_community.vectorstores.cassandra import Cassandra
from langchain_community.llms import Ollama
from langchain.prompts import PromptTemplate
from langchain.chains import RetrievalQA
from langchain.chains import LLMChain

from langchain.chains.combine_documents.stuff import StuffDocumentsChain
from langchain_experimental.text_splitter import SemanticChunker
from services.cassandra_service import CassandraInterface
from flashrank import Ranker 

from langchain_upstage import ChatUpstage,UpstageGroundednessCheck

class AgentInterface:
    """
    A comprehensive agent interface that manages document processing, retrieval, and question-answering capabilities.
    This class implements an advanced RAG (Retrieval-Augmented Generation) system that combines multiple
    retrieval strategies, embedding models, and language models to provide accurate responses to queries
    based on document content.
    Key Features:
    - Multiple document retrieval strategies (BM25, semantic search, parent-child)
    - Ensemble approach combining different retrievers
    - Support for PDF processing with OCR capabilities
    - Integration with various storage systems (Cassandra, AstraDB)
    - Conversation memory management
    - Multiple LLM support (Ollama, Gemini)
        role (str): The role specification for the agent
        UPLOAD_DIR (Path): Directory path for document uploads
        MODEL_NAME_llm (str): Name of the LLM model
        pdfservice (PDFService): Service for PDF operations
        astra_db_store (Union[AstraDBVectorStore, Cassandra]): Vector store for embeddings
        llm_gemini (ChatGoogleGenerativeAI): Gemini model instance
        documents (list): List of loaded documents
        parent_store (InMemoryStore): Storage for parent documents
        ensemble_retriever (EnsembleRetriever): Final ensemble retriever
        memory (ConversationSummaryMemory): Conversation history memory
        retrieval_chain: Configured retrieval chain for QA
        >>> agent = AgentInterface(role="technical_assistant")
        >>> answer = agent.answer_question("What is RAG?", request_obj, "session_123")
        Requires appropriate environment variables to be set for model names,
        API endpoints, and authentication tokens.
    """
    def __init__(self,role,cassandra_intra):
        """
        Initialize the AgentService class with various components for document processing and retrieval.
        This service handles document processing, embedding, vector storage, and retrieval operations
        using multiple models and retrievers in an ensemble approach.
        Args:
            role (str): The role specification for the agent service
        Attributes:
            role (str): Stored role specification
            UPLOAD_DIR (Path): Directory path for uploading documents
            MODEL_NAME_llm (str): Name of the LLM model from environment variables
            BASE_URL_OLLAMA (str): Base URL for Ollama service
            MODEL_NAME_EMBEDDING (str): Name of the embedding model
            MODEL_KWARGS_EMBEDDING (dict): Configuration for embedding model
            ENCODE_KWARGS (dict): Configuration for encoding
            pdfservice (PDFService): Service for handling PDF operations
            CassandraInterface (CassandraInterface): Interface for Cassandra operations
            hf_embedding (HuggingFaceEmbeddings): Hugging Face embeddings model
            semantic_chunker (SemanticChunker): Chunker for semantic text splitting
            astra_db_store (AstraDBVectorStore|Cassandra): Vector store for document embeddings
            llm_gemini (ChatGoogleGenerativeAI): Google's Gemini model instance
            documents (list): Loaded PDF documents
            parent_store (InMemoryStore): In-memory storage for parent documents
            retrieval (BaseRetriever): Base retriever instance
            ensemble_retriever_new (EnsembleRetriever): Combined retriever instance
            multi_retriever (MultiQueryRetriever): Multi-query retrieval system
            ensemble_retriever (EnsembleRetriever): Final ensemble retrieval system
            compression_retriever (ContextualCompressionRetriever): Compression-enabled retriever
            combine_documents_chain (Optional): Chain for combining documents
            llm (Ollama): Ollama model instance
            memory (ConversationSummaryMemory): Memory system for conversation history
        Raises:
            Exception: If AstraDBVectorStore initialization fails, falls back to Cassandra
        """
        
        self.role=role
        self.setup_logging()
        self.prompt=None
        self.cache = OrderedDict()
        self.cache_ttl = 300
        self.UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR"))  
        self.MODEL_NAME_llm=os.getenv("MODEL_NAME")
        self.BASE_URL_OLLAMA=os.getenv("OLLAMA_BASE_URL")
        self.MODEL_NAME_EMBEDDING=os.getenv("MODEL_NAME_EMBEDDING")
        self.MODEL_KWARGS_EMBEDDING={"device": "cpu"}
        self.ENCODE_KWARGS={"normalize_embeddings": True}
        self.pdfservice=PDFService(self.UPLOAD_DIR)
        Ranker(model_name="ms-marco-MiniLM-L-12-v2")
        compressor = FlashrankRerank(model="ms-marco-MiniLM-L-12-v2", top_n=3)
        self.CassandraInterface=cassandra_intra
        self.hf_embedding = HuggingFaceEmbeddings(model_name="BAAI/bge-large-en")
        
        self.semantic_chunker = SemanticChunker (
        self.hf_embedding, 
        )
        self.setup_vector_store()
            
        self.llm_gemini = ChatGoogleGenerativeAI(
            model="gemini-1.5-pro",
            google_api_key=os.getenv("LANGSMITH_API_KEY"),
            temperature=0,
            max_tokens=None,
            timeout=None,
            max_retries=2,
            safety_settings={
                HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
            },
        )
        self.chat_up=ChatUpstage()

        self.groundedness_check = UpstageGroundednessCheck()
        
        self.documents,self.summaries,self.docs_ids=self.load_pdf_documents_fast()
        self.parent_store = InMemoryStore()

        self.compression_retriever= self.setup_ensemble_retrievers(compressor)
       
     
        self.combine_documents_chain=None

        self.llm=Ollama(model=self.MODEL_NAME_llm, base_url=self.BASE_URL_OLLAMA,verbose=True,callback_manager=CallbackManager([StreamingStdOutCallbackHandler()]),)
        self.memory = ConversationSummaryMemory(llm=self.llm_gemini,memory_key="chat_history",return_messages=True)
        
        self.chain=self.retrieval_chain(self.llm_gemini)
    def setup_logging(self):
        logging.basicConfig(level=logging.INFO)
        self.logger = logging.getLogger(__name__)
    def cache_answer(self, question, answer):
        current_time = time.time()
        self.cache[question] = (answer, current_time)
        self.cleanup_cache()
    def cleanup_cache(self):
        current_time = time.time()
        keys_to_delete = [key for key, (_, timestamp) in self.cache.items() if current_time - timestamp >= self.cache_ttl]
        for key in keys_to_delete:
            del self.cache[key]
    
    def setup_ensemble_retrievers(self, compressor):
        """
        Sets up an ensemble of retrievers for enhanced document retrieval.
        This method configures multiple retrievers and combines them using ensemble and compression techniques:
        - BM25 retriever for keyword-based search
        - Parent-child retriever for hierarchical document structure
        - AstraDB retriever with MMR search
        - Multi-query retriever using Gemini LLM
        - Final ensemble combining different retrieval approaches with compression
        Args:
            compressor: A compressor instance used for contextual compression of retrieved documents
        Returns:
            None - Sets up instance attributes for various retrievers
        Instance Attributes Set:
            bm25_retriever: BM25-based retriever instance
            parent_retriever: Parent-child document structure retriever
            retrieval: AstraDB retriever with MMR search
            ensemble_retriever_new: First level ensemble combining parent and AstraDB retrievers
            multi_retriever: Multi-query retriever using the first ensemble
            ensemble_retriever: Final ensemble combining BM25 and multi-query retrievers
            compression_retriever: Compressed version of the final ensemble retriever
        """
        bm25_retriever=BM25Retriever.from_documents(self.documents)
        bm25_retriever.k=2 
        parent_retriever=self.configure_parent_child_splitters()
        
        self.add_documents_to_parent_retriever(parent_retriever)


        retrieval=self.astra_db_store.as_retriever(
            search_type="mmr",
            search_kwargs={'k': 5, 'fetch_k': 50}
        )
        ensemble_retriever_new = EnsembleRetriever(retrievers=[parent_retriever, retrieval],
                                        weights=[0.4, 0.6])
        multi_retriever = MultiQueryRetriever.from_llm(
            ensemble_retriever_new
         , llm=self.chat_up
        )
        ensemble_retriever = EnsembleRetriever(retrievers=[bm25_retriever, multi_retriever],
                                        weights=[0.4, 0.6])
        compression_retriever = ContextualCompressionRetriever(
        base_compressor=compressor,
        base_retriever=ensemble_retriever
        )
        return compression_retriever

    def add_documents_to_parent_retriever(self, parent_retriever):
        parent_retriever.vectorstore.aadd_documents(
        documents=self.documents,
        )
        parent_retriever.vectorstore.aadd_documents(
        documents=self.summaries,
        )
        
        parent_retriever.docstore.mset(list(zip(self.docs_ids, self.documents)))
        for i,doc in enumerate(self.documents):
            doc.metadata["doc_id"] =self.docs_ids[i]
        
        parent_retriever.vectorstore.aadd_documents(
        documents=self.documents,
        )
    def get_cached_answer(self, question):
        current_time = time.time()
        if question in self.cache:
            answer, timestamp = self.cache[question]
            if current_time - timestamp < self.cache_ttl:
                return answer
        return None
    def setup_vector_store(self) -> None:
        """
        Sets up the vector store for document storage and retrieval.

        This method initializes either an AstraDB vector store or falls back to a Cassandra vector store
        if AstraDB initialization fails. It handles the configuration of the storage backend used for
        storing document embeddings.

        Primary configuration:
        - Attempts to initialize AstraDBVectorStore with provided environment variables
        - Clears existing vectors in the store upon initialization

        Fallback configuration (on failure):
        - Initializes HuggingFace embeddings with 'all-MiniLM-L6-v2' model
        - Sets up Cassandra vector store as backup storage solution
        - Clears existing tables before initialization

        Raises:
            Exception: Prints error message if AstraDBVectorStore initialization fails

        Returns:
            None
        """
        try:
            self.astra_db_store = AstraDBVectorStore(
            collection_name="langchain_unstructured",
            embedding=self.hf_embedding,
            token=os.getenv("ASTRA_DB_APPLICATION_TOKEN"),
            api_endpoint=os.getenv("ASTRA_DB_API_ENDPOINT")
            )
            self.astra_db_store.clear()
        except Exception as e:
            print(f"Error initializing AstraDBVectorStore: {e}")
            self.hf_embedding=HuggingFaceEmbeddings(
                model_name="all-MiniLM-L6-v2",
                model_kwargs={'device': 'cpu'},
                encode_kwargs={'normalize_embeddings': True}
            )
            self.CassandraInterface.clear_tables()
            self.astra_db_store :Cassandra = Cassandra(embedding=self.hf_embedding, session=self.session, keyspace=CassandraInterface().KEYSPACE, table_name="vectores_new")
        

    def retrieval_chain(self, llm=None):
        """Creates and returns a retrieval chain for question answering.

        The chain combines a compression retriever, prompt template, and Gemini LLM model to:
        1. Retrieve relevant context using compression retriever
        2. Format a prompt with the context and user question
        3. Generate an answer using the Gemini LLM
        4. Parse the output to a string

        Returns:
            Runnable: A composed chain that accepts a question as input and returns a string answer
            based only on the retrieved context. Returns "I don't know" if unable to answer.

        Example:
            chain = agent.retrieval_chain()
            answer = chain.invoke("What is X?")"""
        
        self.prompt = """
    You are Mohamed Aziz Werghi, a skilled full-stack developer and MLOps engineer. Your task is to answer questions based on the provided context, ensuring that responses are **accurate, well-structured, and visually appealing**. 

    ### Response Guidelines:
    - Format responses using **HTML** for clear presentation.
    - Use **CSS styles** to enhance readability (e.g., fonts, colors, spacing).
    - Use headings (`<h2>`, `<h3>`), lists (`<ul>`, `<ol>`), and tables (`<table>`) where appropriate.
    - Ensure code snippets are wrapped in `<pre><code>` blocks for proper formatting.
    - If styling is necessary, include minimal **inline CSS** or suggest appropriate classes.

    #### Role: Mohamed Aziz Werghi 🤖  
    **Context:**  
    {context}  

    **Chat History:**  
    {chat_history}  

    **Question:**  
    {question}  

    **Your answer (in well-structured HTML & CSS format):**
    """

        
        chain = (
            {
                "context": self.compression_retriever,
                "chat_history": lambda _: "\n".join(
                    [msg.content for msg in self.memory.load_memory_variables({}).get("chat_history", [])]
                ) if self.memory.load_memory_variables({}).get("chat_history") else "",  # Handle empty history
                "question": RunnablePassthrough()
            }
            | PromptTemplate.from_template(self.prompt)
            | self.chat_up
            | StrOutputParser()
        )

        return chain
    def configure_parent_child_splitters(self):
        """
        Configures and returns a parent-child document retrieval system using token-based text splitters.

        This method sets up a hierarchical document retrieval system with:
        - A parent splitter that creates large chunks (512 tokens)
        - A child splitter that creates smaller chunks (128 tokens) 
        - Links these splitters to vector and document stores

        Returns:
            ParentDocumentRetriever: A configured retriever that can fetch both parent and child documents,
                using the specified splitting strategy and connected to the instance's storage systems.
        """
        parent_splitter = TokenTextSplitter(chunk_size=512, chunk_overlap=0)
        child_splitter = TokenTextSplitter(chunk_size=128, chunk_overlap=0)
        parent_retriever = ParentDocumentRetriever(
        vectorstore=self.astra_db_store,
        docstore=self.parent_store,
        child_splitter=child_splitter,
        parent_splitter=parent_splitter,
        )
        return parent_retriever
     
    def answer_rewriting(self,context, query: str) -> str:
        query_rewrite_prompt = """
            You are a helpful assistant that takes a user's question and 
            provided context to generate a clear and direct answer. 
            Please provide a concise response based on the context without adding any extra comments.
            provide a short and clear answer based on the context provided.

            Context: {context}

            Question: {query}

            Answer:
        """

        QA_CHAIN_PROMPT = PromptTemplate(template=query_rewrite_prompt, input_variables=["query"])
        llm_chain = LLMChain(
            llm=self.llm, 
            prompt=QA_CHAIN_PROMPT, 
            callbacks=None, 
            verbose=True
        )
        retrieval_query = llm_chain.invoke({"query": query, "context": context})
        return retrieval_query['text']
   
    def load_pdf_documents_fast(self,bool=True):
        import pandas as pd 
        """
        Loads and processes PDF documents from a specified directory with semantic chunking.
        This method reads all PDF files from the UPLOAD_DIR directory, processes them using
        PDFPlumberLoader, and splits the documents into semantic chunks.
        Parameters:
            bool (bool): Flag to control document loading. If True, loads and processes documents.
                        Defaults to True.
        Returns:
            list: A list of processed document chunks. Returns empty list if bool is False.
        Note:
            - Creates UPLOAD_DIR if it doesn't exist
            - Only processes files with '.pdf' extension
            - Applies semantic chunking to each loaded document
            - Skips any None documents during processing
        """
        documents = []
        summaries = []
        docs_ids=[]
        chain = (
        {"doc": lambda x: x.page_content}
        | ChatPromptTemplate.from_template("Summarize the following document:\n\n{doc}")
        | self.chat_up
        | StrOutputParser()
        )
        if bool==True:
            self.UPLOAD_DIR.mkdir(exist_ok=True) 
            docs=[]
            for file in os.listdir(self.UPLOAD_DIR):
                if   file.endswith(".pdf")==True and os.path.isfile(self.UPLOAD_DIR / file):
                    
                        full_path = self.UPLOAD_DIR/ file
                        loader = PDFPlumberLoader(full_path)
                        document = loader.load()
                        docs.append(document)
                self.load_csv_to_documents(pd, docs, file)
            for doc in docs:
                if doc is not None:
                    print(doc)
                    chunks = self.semantic_chunker.split_documents(doc)
                    summaries_x = chain.batch(chunks, {"max_concurrency": 5})
                    doc_ids = [str(uuid.uuid4()) for _ in chunks]
                    summary_docs = [
                        Document(page_content=s, metadata={"doc_id": doc_ids[i]})
                        for i, s in enumerate(summaries_x)
                    ]

                    documents.extend(chunks)  
                    summaries.extend(summary_docs)
                    docs_ids.extend(doc_ids)
            
        #self.load_documents_from_cassandra(documents)
                
        return documents, summaries,docs_ids

    def load_csv_to_documents(self, pd, docs, file):
        if  file.endswith(".csv") ==False:
            file_completed = os.path.join(self.UPLOAD_DIR, file)
            df = pd.read_csv(file_completed)

            df['meta'] = df.apply(lambda row: {"source": file_completed}, axis=1)
            records = df.to_dict('records')
            columns = df.columns
            ds=[]
            for record in records:
                full_text = "\n".join([f"{col}: {record[col]}" for col in columns])

                meta = record.get('meta', {})  # Ensure metadata is retrieved safely
                doc = Document(page_content=full_text, metadata=meta)

                ds.append(doc) 
            docs.append(ds)
    def load_pdf_v2(self):
        documents = []
        for filename in os.listdir(self.UPLOAD_DIR):
            file_path = self.UPLOAD_DIR / filename
            if not file_path.is_file():
                continue
            elements = unstructured.get_elements_from_api(
                file_path=file_path,
                api_key=os.getenv("UNSTRUCTURED_API_KEY"),
                api_url=os.getenv("UNSTRUCTURED_API_URL"),
                strategy="fast", # default "auto"
                pdf_infer_table_structure=True,
            )

            documents.extend(self.process_elements_to_documents(elements))
        return documents

    def process_elements_to_documents(self,elements):
        documents = []
        current_doc = None
        for el in elements:
            if el.category in ["Header", "Footer"]:
                continue # skip these
            if el.category == "Title":
                if current_doc is not None:
                    documents.append(current_doc)
                current_doc = None
            if not current_doc:
                current_doc = Document(page_content="", metadata=el.metadata.to_dict())
            current_doc.page_content += el.metadata.text_as_html if el.category == "Table" else el.text
            if el.category == "Table":
                if current_doc is not None:
                    documents.append(current_doc)
                current_doc = None
        return documents
    def load_pdf_documents_with_ocr(self, bool=True):
        """
        Load PDF documents and convert them to text format, including both regular text and text extracted from images.
        This method processes PDF documents stored in the upload directory, combining the regular text content
        with text extracted from images found in the PDFs. Each document is converted into a Document object
        with associated metadata.
        Args:
            bool (bool, optional): Flag to control whether documents should be loaded. Defaults to True.
        Returns:
            list: A list of Document objects, where each Document contains:
                - page_content: Combined text from both regular content and image-extracted text
                - metadata: Associated metadata dictionary for the document
        Note:
            - Creates upload directory if it doesn't exist
            - Uses PDFService to extract data from PDF files
            - Combines regular text and image-extracted text with newline separators
            - Validates metadata is in dictionary format
        """
        documents = []
        if bool==True:
            self.UPLOAD_DIR.mkdir(exist_ok=True) 
            records=self.pdfservice.extract_data_from_pdf_directory()
            docs=[]
            for record in records:
                
                full_text=record['text'] + "\n" +  "\n" + record['images_to_text']
                meta = record['meta'] if isinstance(record['meta'], dict) else {}
                doc = Document(page_content=full_text, metadata=meta)
                docs.append(doc)

            documents.extend(docs)
            
        #self.load_documents_from_cassandra(documents)
                
        return documents
    
    def load_documents_from_cassandra(self, documents):
        schemas = self.CassandraInterface.retrieve_column_descriptions()
            
        for table_name,desc in schemas.items():
            loader=CassandraLoader(
                table=table_name,
                session=self.CassandraInterface.session,
                keyspace=self.CassandraInterface.KEYSPACE,
                )
            docs=loader.load()
            if table_name!="vectores":
                    for doc in docs:
                        if doc is not None:
                            doc.metadata['source'] = f'Description:{desc}.{table_name}'
            documents.extend(docs)
    def answer_question(self,question:str,request,session_id):
        """
        Process a user question and return an answer using either cached responses or generating a new one.

        This method first checks if an identical question has been answered before. If found, returns
        the cached response. Otherwise, processes the question through a retrieval chain to generate
        a new answer, stores it, and returns the result.

        Parameters:
            question (str): The user's question to be answered
            request: The original request object 
            session_id: Unique identifier for the user session

        Returns:
            dict: A dictionary containing:
                - request: The original request object
                - answer (str): The response to the question
                - question (str): The original question
                - partition_id (str, optional): ID of the cached response partition if exists
                - timestamp (datetime, optional): Timestamp of the cached response if exists
                - evaluation (bool): Always True, indicates response evaluation status

        Raises:
            Any exceptions from underlying services (CassandraInterface, retrieval_chain) are not explicitly handled
        """
        self.logger.info(f"Received question: {question}")
        exist_answer=self.get_cached_answer(question)
        if exist_answer is not None:
            return self.generate_message_html(question, exist_answer)
        else:
            question_enhanced= question 
            final_answer=None
            
            try:
                context=self.compression_retriever.invoke(question_enhanced)
                context_memory= self.memory.load_memory_variables({}).get("chat_history", [])
                if context_memory:
                    context_memory = "\n".join([msg.content for msg in context_memory])
                    context = f"{context}\n{context_memory}"
                final_answer = self.chain.invoke(question_enhanced)
                self.logger.info(f"Answer provided: {final_answer}")
                gc_result=self.groundedness_check.invoke({
                "context": context,
                "answer": final_answer,
                })
                if gc_result.lower().startswith("grounded"):
                    print("Grounded")
                else:
                    print("Not Grounded")
            except Exception as e:
                self.logger.error(f"Error while answering question: {e}")
                self.chain=self.retrieval_chain(self.chat_up)
                final_answer = self.chain.invoke(question_enhanced)
            #final_answer= self.answer_rewriting(f"{final_answer}",question)
            self.cache_answer(question, final_answer)
            self.memory.save_context({"question": question}, {"answer": f"{final_answer}"})
            
            self.CassandraInterface.insert_answer(session_id,question,final_answer)
            return self.generate_message_html(question, final_answer)

    def generate_message_html(self, question, final_answer):
        message_html = f"""
            <div class="message user">
                <input type="hidden" id="partition_id" name="partition_id" value="{uuid.uuid1()}">
                <div class="message-icon">
                <img src="/static/icons8-user.svg" alt="bot" class="bot-icon">
                </div>
                <div class="message-content">
                <p>{question}</p>
                </div>
            </div>
            <div class="message ai">
                    <div class="message-icon">
                    <img src="/static/bot.png" alt="bot" class="bot-icon">
                    </div>
                    {final_answer}
             </div>
            """
        
        return HTMLResponse(content=message_html, status_code=200)
