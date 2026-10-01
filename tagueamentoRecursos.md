# **Tagueamento de recursos AWS \- Time AI**

## Nosso code

Aqui no time usamos esse “code” da AWS: 7b0pljcsg0gii1a5gn5vfca0v  
esse id é específico para recursos de AI: **Bedrock**, AgentCore, SageMaker, Comprehend  
Esse tutorial usa ele para facilitar.

## Por que taguear?

AWS mede os parceiros pelo billing gerado nos clientes. Por isso vamos usar Tag ou UserAgent para AWS poder contabilizar.

## Taguear recursos

Em quase tudo podemos colocar Tag. Ideal é usar IaC e um tagueamento global.

Tag Key: aws-apn-id

Tag Value: pc:7b0pljcsg0gii1a5gn5vfca0v

ou seja:

aws-apn-id: pc:7b0pljcsg0gii1a5gn5vfca0v

## Anotação de chamadas LLM

Ao invés de criar application inference profiles toda vez, podemos “taguear” chamadas individuais. Isso não vai ser refletido no Cost Explorer como tag, mas é suficiente para o nosso PRM.

Precisamos anotar chamadas de LLMs com a tag   
"user\_agent": "APN\_1.1/pc\_\<AWS Marketplace product-code\>$"  
nosso code é 7b0pljcsg0gii1a5gn5vfca0v então seria assim:  
"user\_agent": "APN\_1.1/pc\_7b0pljcsg0gii1a5gn5vfca0v$"

### Exemplo Python no formato Bedrock e LiteLLM (OpenAI format)

fonte: [https://docs.aws.amazon.com/PRM/latest/aws-prm-onboarding-guide/user-agent-samples.html](https://docs.aws.amazon.com/PRM/latest/aws-prm-onboarding-guide/user-agent-samples.html) 

criar env var:   
`AWS_SDK_UA_APP_ID = APN_1.1/pc_7b0pljcsg0gii1a5gn5vfca0v$`

```py
import os
from langchain_openai import ChatOpenAI
from botocore.config import Config
from langchain_aws import ChatBedrockConverse

# 1. Fetch the custom user agent value from your environment variables
custom_user_agent = os.getenv("AWS_SDK_UA_APP_ID", "APN_1.1/pc_7b0pljcsg0gii1a5gn5vfca0v$")

# 2. Initialize a LiteLLM model
# Pass the custom User-Agent inside the default_headers dictionary
model_liteLLM = ChatOpenAI(
    model="global.anthropic.claude-sonnet-4-6",  # Replace with your LiteLLM model routing name
    api_key="your-litellm-api-key", 
    base_url="http://localhost:4000",  # Your LiteLLM proxy URL
    default_headers={
        "User-Agent": custom_user_agent
    }
)

# 3. Initialize a Bedrock model
model_bedrock = ChatBedrockConverse(
    model="global.anthropic.claude-sonnet-4-6",  # Your exact Bedrock ID
    region_name="us-east-1",                            # Your AWS region
    config=Config(user_agent_extra=custom_user_agent)   # Injects the custom user-agent header
)

model = model_liteLLM # model_liteLLM or model_bedrock
```

agora é só usar como sempre:

```py
from typing import Annotated
from typing_extensions import TypedDict

from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages

# 3. Define a simple LangGraph state and node
class State(TypedDict):
    messages: Annotated[list, add_messages]

def call_llm_node(state: State):
    # The model invocation will automatically include the custom User-Agent header
    response = model.invoke(state["messages"])
    return {"messages": [response]}

# 4. Build the LangGraph workflow
workflow = StateGraph(State)
workflow.add_node("agent", call_llm_node)
workflow.add_edge(START, "agent")
workflow.add_edge("agent", END)

app = workflow.compile()

# 5. Execute the graph
if __name__ == "__main__":
    inputs = {"messages": [("user", "Hello! Write a 1-sentence welcome message.")]}
    config = {"configurable": {"thread_id": "1"}}
    
    for output in app.stream(inputs, config, stream_mode="values"):
        for message in output.get("messages", []):
            message.pretty_print()
```

