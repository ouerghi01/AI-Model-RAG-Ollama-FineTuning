
from dotenv import load_dotenv
import os
import jwt
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jwt.exceptions import InvalidTokenError
from passlib.context import CryptContext
from pydantic import BaseModel
from typing import Annotated
from fastapi import Request, Form

load_dotenv()  # take environment variables from .env.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")
SECRET_KEY = os.getenv("SEC_API", "default_secret_key_for_development_only")
if SECRET_KEY is None:
    raise ValueError("SEC_API environment variable must be set")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30
class Token(BaseModel):
    access_token: str
    token_type: str
    the_user:str
class TokenData(BaseModel):
    username: str | None = None


class User(BaseModel):
    username: str
    email: str | None = None
    his_job: str | None = None
    hashed_password: str

class AuthService:
     
    def __init__(self,cassandra_intra):
         
        self.pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
        self.cassandra_intra=cassandra_intra

        self.oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")
    def verify_password(self,plain_password, hashed_password):
        return self.pwd_context.verify(plain_password, hashed_password)


    def get_password_hash(self,password):
        return self.pwd_context.hash(password)
    async def register_user(self, request: Request, username: str = Form(...), email: str = Form(...), password: str = Form(...), his_job: str = Form(...)):
            hashed_password = self.get_password_hash(password)
            user = User(username=username, email=email, his_job=his_job, hashed_password=hashed_password)
            self.cassandra_intra.insert_user(user.username, user.email, user.his_job, user.hashed_password)
            return user
    def get_user(self, username: str):
        user_found=self.cassandra_intra.find_user(username)
        if user_found:
            return User(
                username=user_found.username,
                email=user_found.email,
                his_job=user_found.his_job,
                hashed_password=user_found.password,
            )
        return None
    def authenticate_user(self, username: str, password: str):
        user = self.get_user( username)
        if not user:
            return False
        if not self.verify_password(password, user.hashed_password):
            return False
        return user
    async def login_for_access_token(self,
            form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
        ) -> Token:
        user = self.authenticate_user( form_data.username, form_data.password)
        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Incorrect username or password",
                headers={"WWW-Authenticate": "Bearer"},
            )
        access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
        access_token = self.create_access_token(
            data={"sub": user.username}, expires_delta=access_token_expires
        )
        the_user=await self.get_current_user(access_token)
        return Token(access_token=access_token, token_type="bearer", the_user=the_user.username)

    def create_access_token(self,data: dict, expires_delta: timedelta | None = None):
        to_encode = data.copy()
        if expires_delta:
            expire = datetime.now(timezone.utc) + expires_delta
        else:
            expire = datetime.now(timezone.utc) + timedelta(minutes=15)
        to_encode.update({"exp": expire})
        encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
        return encoded_jwt
    async def get_current_user(self,token: Annotated[str, Depends(oauth2_scheme)]):
        credentials_exception = HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
        try:
            payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
            username = payload.get("sub")
            if username is None:
                raise credentials_exception
            token_data = TokenData(username=username)
        except InvalidTokenError:
            raise credentials_exception
        user = self.get_user( username=token_data.username)
        if user is None:
            raise credentials_exception
        return user
    