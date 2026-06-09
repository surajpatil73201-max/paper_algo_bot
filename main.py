from fastapi import FastAPI
app = FastAPI()

@app.get('/')
def home():
    return {'status':'Paper Algo Bot Running'}
