from langchain_community.document_loaders import CassandraLoader
from langchain.retrievers import ContextualCompressionRetriever
import logging
import time
from collections import OrderedDict
from langchain_community.document_compressors import FlashrankRerank
from langchain_core.callbacks import CallbackManager
from langchain_core.callbacks import StreamingStdOutCallbackHandler
from langchain_google_genai import (
    ChatGoogleGenerativeAI,
    HarmBlockThreshold,
    HarmCategory,
)
from fastapi.responses import HTMLResponse
import uuid
from langchain.retrievers.multi_query import MultiQueryRetriever
from langchain.storage import InMemoryStore
from langchain_text_splitters import TokenTextSplitter
from langchain.retrievers import ParentDocumentRetriever
from langchain_astradb import AstraDBVectorStore
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import PromptTemplate
from langchain_core.runnables import RunnablePassthrough
from langchain.memory import ConversationSummaryMemory
from dotenv import load_dotenv
import os 
from pathlib import Path
from langchain.retrievers import  EnsembleRetriever

from langchain_community.retrievers import BM25Retriever


from load_data import DataLoader

load_dotenv()  # take environment variables from .env.
os.environ["UPSTAGE_API_KEY"] = os.getenv("UPSTAGE_API_KEY")
from langchain_community.embeddings import HuggingFaceEmbeddings  
from langchain_community.vectorstores.cassandra import Cassandra
from langchain_community.llms import Ollama
from langchain_core.prompts import PromptTemplate
from langchain.chains import LLMChain

from langchain_experimental.text_splitter import SemanticChunker
from cassandra_service import CassandraInterface
from flashrank import Ranker 
import json
from langchain_upstage import ChatUpstage,UpstageGroundednessCheck
from langchain_core.prompts import PromptTemplate
from langchain_core.pydantic_v1 import BaseModel, Field
from langchain_core.output_parsers import JsonOutputParser
class Answer(BaseModel):
    reponse: str = Field(description="The answer to the question")
class Answers(BaseModel):
    answers: list[Answer] = Field(description="List of answers to the questions")
    # Set up a parser + inject instructions into the prompt template.
parser = JsonOutputParser(pydantic_object=Answers)

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
    def __init__(self,role="assistant",cassandra_intra:CassandraInterface=CassandraInterface(),name_dir="/home/aziz/IA-DeepSeek-RAG-IMPL/APP/uploads"
        ):
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
        self.name_dir=name_dir
        self.UPLOAD_DIR = Path(self.name_dir) 
        self.load_data=DataLoader(upload_dir=self.UPLOAD_DIR) 
        
        self.MODEL_NAME_llm=os.getenv("MODEL_NAME")
        self.BASE_URL_OLLAMA=os.getenv("OLLAMA_BASE_URL")
        self.MODEL_NAME_EMBEDDING=os.getenv("MODEL_NAME_EMBEDDING")
        self.MODEL_KWARGS_EMBEDDING={"device": "cpu"}
        self.ENCODE_KWARGS={"normalize_embeddings": True}
        # Create a single Ranker instance properly
        ranker = Ranker(model_name="ms-marco-MiniLM-L-12-v2")
        self.compressor = FlashrankRerank(
            client=ranker
        )
        self.cassandraInterface=cassandra_intra
        #self.cassandraInterface.clear_tables()
        self.hf_embedding = None
        self.semantic_chunker = SemanticChunker (
        self.hf_embedding, 
        )
        self.setup_vector_store()

        self.llm_gemini = ChatGoogleGenerativeAI(
            model="gemini-2.0-flash",
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

        self.documents,self.docs_ids=  [],[]
        self.parent_store = InMemoryStore()

        self.compression_retriever=  None
        self.combine_documents_chain=None
        self.llm=Ollama(model=self.MODEL_NAME_llm, base_url=self.BASE_URL_OLLAMA,verbose=True,callback_manager=CallbackManager([StreamingStdOutCallbackHandler()]),)
        self.memory = ConversationSummaryMemory(llm=self.llm_gemini,memory_key="chat_history",return_messages=True)

        self.chain=None
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
    def evaluate(self, html_output):
        prompt = """
        Evaluate the following HTML structure for correctness and completeness. 
        Consider the following criteria:
        1. Valid HTML syntax
        2. Presence of essential elements (html, head, body)
        3. Proper nesting of elements
        4. Use of semantic HTML tags where appropriate

        HTML to evaluate:
        {html_output}

        Provide a JSON response with the following structure:

        
            "is_valid": true/false,
            "issues": ["list", "of", "identified", "issues"],
            "suggestions": ["list", "of", "recommended", "improvements"],
            "improved_html": "new, improved HTML code with fixes"
        

        In your response:
        - "is_valid" should be a boolean indicating whether the HTML is correct or not.
        - "issues" should be an array listing any errors, missing elements, or misused tags.
        - "suggestions" should be an array offering practical suggestions for improvement or better practices.
        - "improved_html" should be the refined version of the HTML with improvements based on the identified issues and suggestions.

        Please ensure that the improved HTML reflects best practices and correct syntax.
        """


        QA_CHAIN_PROMPT = PromptTemplate(template=prompt, input_variables=["html_output"])
        llm_chain = LLMChain(
            llm=self.llm_gemini, 
            prompt=QA_CHAIN_PROMPT, 
            callbacks=None, 
            verbose=True
        )
        evaluation = llm_chain.invoke({"html_output": html_output})
        text=evaluation['text'].replace("json","")
        text=text.replace("```","")
        reponse= json.loads(text) if evaluation else {"is_valid": False, "issues": ["Evaluation failed"], "suggestions": []}
        return reponse
    async def setup_ensemble_retrievers(self):
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
        
        #bm25_retriever=BM25Retriever.from_documents(self.documents)
        #bm25_retriever.k=2 
        parent_retriever=self.configure_parent_child_splitters()
        await ( self.add_documents_to_parent_retriever(parent_retriever))
        retrieval=self.astra_db_store.as_retriever(
            search_type="mmr",
            search_kwargs={'k': 5, 'fetch_k': 50}
        )
        
        ensemble_retriever_new = EnsembleRetriever(retrievers=[parent_retriever, retrieval],
                                        weights=[0.4, 0.6])
        multi_retriever = MultiQueryRetriever.from_llm(
            ensemble_retriever_new
         , llm=self.llm
        )
        ensemble_retriever = EnsembleRetriever(retrievers=[ensemble_retriever_new, multi_retriever],
                                        weights=[0.5, 0.6])
        compression_retriever = ContextualCompressionRetriever(
        base_compressor=self.compressor,
        base_retriever=ensemble_retriever
        )
        return compression_retriever

    async def add_documents_to_parent_retriever(self, parent_retriever:ParentDocumentRetriever):
        parent_retriever.docstore.mset(list(zip(self.docs_ids, self.documents)))
        for i,doc in enumerate(self.documents):
            doc.metadata["doc_id"] =self.docs_ids[i]

        await parent_retriever.vectorstore.aadd_documents(
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
            self.hf_embedding=HuggingFaceEmbeddings(
                model_name="all-MiniLM-L6-v2",
                model_kwargs={'device': 'cpu'},
                encode_kwargs={'normalize_embeddings': True}
            )
            #self.CassandraInterface.clear_tables()
            self.astra_db_store :Cassandra = Cassandra(embedding=self.hf_embedding, session=self.cassandraInterface.session, keyspace=self.cassandraInterface.KEYSPACE, table_name="vectores_new")

            #self.astra_db_store.clear()
        except Exception as e:
            print(f"Error initializing AstraDBVectorStore: {e}")

            self.astra_db_store = AstraDBVectorStore(
            collection_name="langchain_unstructured",
            embedding=self.hf_embedding,
            token=os.getenv("ASTRA_DB_APPLICATION_TOKEN"),
            api_endpoint=os.getenv("ASTRA_DB_API_ENDPOINT")
            )
            print(f"Error initializing AstraDBVectorStore: {e}")
           
    def simple_chain(self):
       

        self.prompt = """
    You are Mohamed Aziz Werghi, a skilled In cassandra database. Your task is to answer questions based on the provided context, ensuring that responses are **accurate, well-structured, and visually appealing**. 

    ### Response Guidelines:
    return list of different  Expected Answers  to this question  in json format 

    #### Role: Mohamed Aziz Werghi 🤖  
    **Context:**  
    {context}  

    **Chat History:**  
    {chat_history}  

    **Question:**  
    {question}  

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
            | self.llm_gemini
            | StrOutputParser() | parser
        )

        return chain
    def retrieval_chain(self):
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
    You are Mohamed Aziz Werghi, a skilled In cassandra database. Your task is to answer questions based on the provided context, ensuring that responses are **accurate, well-structured, and visually appealing**. 

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
                refined_answer = self.evaluate(final_answer)
                final_answer=refined_answer["improved_html"]
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

            self.cassandraInterface.insert_answer(session_id,question,final_answer)
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
                    <div class="message-content">
                    {final_answer}
                    </div>
             </div>
            """

        return HTMLResponse(content=message_html, status_code=200)
#https://medium.com/@irmjoshy/multi-agent-models-for-webpage-development-leveraging-ai-for-code-generation-and-verification-52cb60cec9eb