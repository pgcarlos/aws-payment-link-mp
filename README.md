# AWS Payment Link (FastAPI + DynamoDB + Mercado Pago)

API mínima para generar **links de pago** con **Mercado Pago**, guardar el registro en **DynamoDB** y actualizar su estado vía **webhook**.  
Funciona **100% local** con **DynamoDB Local** (sin cuenta de AWS). Listo para desplegar con **AWS SAM** cuando lo necesites.

## Stack
- Python 3.12, FastAPI, Uvicorn
- Boto3 (DynamoDB)
- Mercado Pago SDK
- SAM (despliegue opcional)

---

## Requisitos

- Python 3.11+ (recomendado 3.12)
- Pip
- Java Runtime (`default-jre`) para **DynamoDB Local**
- (Opcional) AWS CLI + SAM CLI para despliegue

---

## Estructura

aws-payment-link/
├─ app/
│ └─ main.py
├─ local/
│ └─ dynamodb/ # se crea al descargar DynamoDB Local
├─ tests/
│ └─ test_links.py
├─ .env # tu token de MP (MERCADO PAGO)
├─ .gitignore
├─ requirements.txt
└─ template.yaml # SAM (despliegue opcional)
