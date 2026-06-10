==
NVIDIA
==
replace llama-3 instruct with:
https://build.nvidia.com/qwen/qwen3-coder-480b-a35b-instruct
API Ref: https://docs.api.nvidia.com/nim/reference/qwen-qwen3-coder-480b-a35b-instruct
Python:
from openai import OpenAI

client = OpenAI(
  base_url = "https://integrate.api.nvidia.com/v1",
  api_key = "$NVIDIA_API_KEY"
)

completion = client.chat.completions.create(
  model="qwen/qwen3-coder-480b-a35b-instruct",
  messages=[{"role":"user","content":""}],
  temperature=0.7,
  top_p=0.8,
  max_tokens=16384,
  stream=True,
  tools=[{"type":"function","function":{"name":"describe_harry_potter_character","description":"Returns information and images of Harry Potter characters.","parameters":{"type":"object","properties":{"name":{"type":"string","enum":["Harry James Potter","Hermione Jean Granger","Ron Weasley","Fred Weasley","George Weasley","Bill Weasley","Percy Weasley","Charlie Weasley","Ginny Weasley","Molly Weasley","Arthur Weasley","Neville Longbottom","Luna Lovegood","Draco Malfoy","Albus Percival Wulfric Brian Dumbledore","Minerva McGonagall","Remus Lupin","Rubeus Hagrid","Sirius Black","Severus Snape","Bellatrix Lestrange","Lord Voldemort","Cedric Diggory","Nymphadora Tonks","James Potter"],"description":"Name of the Harry Potter character"}},"required":["name"]}}},{"type":"function","function":{"name":"name_a_color","description":"A tool that returns a bunch of color names for a given color_hex.","parameters":{"type":"object","properties":{"color_hex":{"type":"string","description":"A hexadecimal color value which must be represented as a string."}},"required":["color_hex"]}}}],
  tool_choice="auto"
)

for chunk in completion:
  if chunk.choices and chunk.choices[0].delta.content is not None:
    print(chunk.choices[0].delta.content, end="")
==
https://build.nvidia.com/z-ai/glm-5.1
API ref: https://docs.api.nvidia.com/nim/reference/z-ai-glm5.1
Python: 
from openai import OpenAI
import os
import sys

_USE_COLOR = sys.stdout.isatty() and os.getenv("NO_COLOR") is None
_REASONING_COLOR = "\033[90m" if _USE_COLOR else ""
_RESET_COLOR = "\033[0m" if _USE_COLOR else ""

client = OpenAI(
  base_url = "https://integrate.api.nvidia.com/v1",
  api_key = "$NVIDIA_API_KEY"
)


completion = client.chat.completions.create(
  model="z-ai/glm-5.1",
  messages=[{"role":"user","content":""}],
  temperature=1,
  top_p=1,
  max_tokens=16384,
  extra_body={"chat_template_kwargs":{"enable_thinking":True,"clear_thinking":False}},
  stream=True,
  tools=[{"type":"function","function":{"name":"describe_harry_potter_character","description":"Returns information and images of Harry Potter characters.","parameters":{"type":"object","properties":{"name":{"type":"string","enum":["Harry James Potter","Hermione Jean Granger","Ron Weasley","Fred Weasley","George Weasley","Bill Weasley","Percy Weasley","Charlie Weasley","Ginny Weasley","Molly Weasley","Arthur Weasley","Neville Longbottom","Luna Lovegood","Draco Malfoy","Albus Percival Wulfric Brian Dumbledore","Minerva McGonagall","Remus Lupin","Rubeus Hagrid","Sirius Black","Severus Snape","Bellatrix Lestrange","Lord Voldemort","Cedric Diggory","Nymphadora Tonks","James Potter"],"description":"Name of the Harry Potter character"}},"required":["name"]}}},{"type":"function","function":{"name":"name_a_color","description":"A tool that returns a bunch of color names for a given color_hex.","parameters":{"type":"object","properties":{"color_hex":{"type":"string","description":"A hexadecimal color value which must be represented as a string."}},"required":["color_hex"]}}}],
  tool_choice="auto"
)

for chunk in completion:
  if not getattr(chunk, "choices", None):
    continue
  if len(chunk.choices) == 0 or getattr(chunk.choices[0], "delta", None) is None:
    continue
  delta = chunk.choices[0].delta
  reasoning = getattr(delta, "reasoning_content", None)
  if reasoning:
    print(f"{_REASONING_COLOR}{reasoning}{_RESET_COLOR}", end="")
  if getattr(delta, "content", None) is not None:
    print(delta.content, end="")
  if getattr(delta, "tool_calls", None):
    print(delta.tool_calls)
==
https://build.nvidia.com/deepseek-ai/deepseek-v4-pro
APi ref: https://docs.api.nvidia.com/nim/reference/deepseek-ai-deepseek-v4-pro
Python:
from openai import OpenAI

client = OpenAI(
  base_url = "https://integrate.api.nvidia.com/v1",
  api_key = "$NVIDIA_API_KEY"
)


completion = client.chat.completions.create(
  model="deepseek-ai/deepseek-v4-pro",
  messages=[{"role":"user","content":""}],
  temperature=1,
  top_p=0.95,
  max_tokens=16384,
  extra_body={"chat_template_kwargs":{"thinking":True,"reasoning_effort":"max"}}, //"max","high","none"
  stream=True,
  tools=[{"type":"function","function":{"name":"describe_harry_potter_character","description":"Returns information and images of Harry Potter characters.","parameters":{"type":"object","properties":{"name":{"type":"string","enum":["Harry James Potter","Hermione Jean Granger","Ron Weasley","Fred Weasley","George Weasley","Bill Weasley","Percy Weasley","Charlie Weasley","Ginny Weasley","Molly Weasley","Arthur Weasley","Neville Longbottom","Luna Lovegood","Draco Malfoy","Albus Percival Wulfric Brian Dumbledore","Minerva McGonagall","Remus Lupin","Rubeus Hagrid","Sirius Black","Severus Snape","Bellatrix Lestrange","Lord Voldemort","Cedric Diggory","Nymphadora Tonks","James Potter"],"description":"Name of the Harry Potter character"}},"required":["name"]}}},{"type":"function","function":{"name":"name_a_color","description":"A tool that returns a bunch of color names for a given color_hex.","parameters":{"type":"object","properties":{"color_hex":{"type":"string","description":"A hexadecimal color value which must be represented as a string."}},"required":["color_hex"]}}}],
  tool_choice="auto"
)

for chunk in completion:
  if not getattr(chunk, "choices", None):
    continue
  reasoning = getattr(chunk.choices[0].delta, "reasoning", None) or getattr(chunk.choices[0].delta, "reasoning_content", None)
  if reasoning:
    print(reasoning, end="")
  if chunk.choices and chunk.choices[0].delta.content is not None:
    print(chunk.choices[0].delta.content, end="")
  if chunk.choices[0].delta.tool_calls:
    print(chunk.choices[0].delta.tool_calls)
==
https://build.nvidia.com/moonshotai/kimi-k2.6
API Ref: https://docs.api.nvidia.com/nim/reference/moonshotai-kimi-k2-6
Python:
import requests, base64

invoke_url = "https://integrate.api.nvidia.com/v1/chat/completions"
stream = True

def read_b64(path):
  with open(path, "rb") as f:
    return base64.b64encode(f.read()).decode()

headers = {
  "Authorization": "Bearer $NVIDIA_API_KEY",
  "Accept": "text/event-stream" if stream else "application/json"
}

payload = {
  "model": "moonshotai/kimi-k2.6",
  "messages": [{"role":"user","content":""}],
  "max_tokens": 16384,
  "temperature": 1.00,
  "top_p": 1.00,
  "stream": stream,
  "chat_template_kwargs": {"thinking":True},
}
payload["tools"] = [{"type":"function","function":{"name":"describe_harry_potter_character","description":"Returns information and images of Harry Potter characters.","parameters":{"type":"object","properties":{"name":{"type":"string","enum":["Harry James Potter","Hermione Jean Granger","Ron Weasley","Fred Weasley","George Weasley","Bill Weasley","Percy Weasley","Charlie Weasley","Ginny Weasley","Molly Weasley","Arthur Weasley","Neville Longbottom","Luna Lovegood","Draco Malfoy","Albus Percival Wulfric Brian Dumbledore","Minerva McGonagall","Remus Lupin","Rubeus Hagrid","Sirius Black","Severus Snape","Bellatrix Lestrange","Lord Voldemort","Cedric Diggory","Nymphadora Tonks","James Potter"],"description":"Name of the Harry Potter character"}},"required":["name"]}}},{"type":"function","function":{"name":"name_a_color","description":"A tool that returns a bunch of color names for a given color_hex.","parameters":{"type":"object","properties":{"color_hex":{"type":"string","description":"A hexadecimal color value which must be represented as a string."}},"required":["color_hex"]}}}]
payload["tool_choice"] = "auto"

response = requests.post(invoke_url, headers=headers, json=payload, stream=stream)
if stream:
    for line in response.iter_lines():
        if line:
            print(line.decode("utf-8"))
else:
    print(response.json())
==
https://build.nvidia.com/nvidia/nemotron-3-ultra-550b-a55b
API ref: https://docs.api.nvidia.com/nim/reference/nvidia-nemotron-3-ultra-550b-a55b
Python:
from openai import OpenAI

client = OpenAI(
  base_url = "https://integrate.api.nvidia.com/v1",
  api_key = "$NVIDIA_API_KEY"
)


completion = client.chat.completions.create(
  model="nvidia/nemotron-3-ultra-550b-a55b",
  messages=[{"role":"user","content":""}],
  temperature=1,
  top_p=0.95,
  max_tokens=16384,
  extra_body={"chat_template_kwargs":{"enable_thinking":True},"reasoning_budget":16384},
  stream=True,
  tools=[{"type":"function","function":{"name":"describe_harry_potter_character","description":"Returns information and images of Harry Potter characters.","parameters":{"type":"object","properties":{"name":{"type":"string","enum":["Harry James Potter","Hermione Jean Granger","Ron Weasley","Fred Weasley","George Weasley","Bill Weasley","Percy Weasley","Charlie Weasley","Ginny Weasley","Molly Weasley","Arthur Weasley","Neville Longbottom","Luna Lovegood","Draco Malfoy","Albus Percival Wulfric Brian Dumbledore","Minerva McGonagall","Remus Lupin","Rubeus Hagrid","Sirius Black","Severus Snape","Bellatrix Lestrange","Lord Voldemort","Cedric Diggory","Nymphadora Tonks","James Potter"],"description":"Name of the Harry Potter character"}},"required":["name"]}}},{"type":"function","function":{"name":"name_a_color","description":"A tool that returns a bunch of color names for a given color_hex.","parameters":{"type":"object","properties":{"color_hex":{"type":"string","description":"A hexadecimal color value which must be represented as a string."}},"required":["color_hex"]}}}],
  tool_choice="auto"
)

for chunk in completion:
  if not chunk.choices:
    continue
  reasoning = getattr(chunk.choices[0].delta, "reasoning_content", None)
  if reasoning:
    print(reasoning, end="")
  if chunk.choices[0].delta.content is not None:
    print(chunk.choices[0].delta.content, end="")
  if chunk.choices[0].delta.tool_calls:
    print(chunk.choices[0].delta.tool_calls)
==
https://build.nvidia.com/stepfun-ai/step-3.7-flash
API ref: https://docs.api.nvidia.com/nim/reference/stepfun-ai-step-3-7-flash
Python:
from pathlib import Path
import requests, base64

invoke_url = "https://integrate.api.nvidia.com/v1/chat/completions"
stream = True

IMAGE_MIME_TYPES = {
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".webp": "image/webp",
}

def find_image_path(index):
  for suffix in IMAGE_MIME_TYPES:
    path = Path(f"image_{index}{suffix}")
    if path.exists():
      return path
  raise FileNotFoundError(f"Expected image_{index}.png/.jpg/.jpeg/.webp")

def read_image_data_url(path):
  with open(path, "rb") as f:
    image_b64 = base64.b64encode(f.read()).decode()
  return f"data:{IMAGE_MIME_TYPES[path.suffix.lower()]};base64,{image_b64}"


headers = {
  "Authorization": "Bearer $NVIDIA_API_KEY",
  "Accept": "text/event-stream" if stream else "application/json"
}

payload = {
  "model": "stepfun-ai/step-3.7-flash",
  "messages": [{"role":"user","content":""}],
  "max_tokens": 16384,
  "temperature": 1.00,
  "top_p": 0.95,
  "stream": stream,
  
}
payload["tools"] = [{"type":"function","function":{"name":"describe_harry_potter_character","description":"Returns information and images of Harry Potter characters.","parameters":{"type":"object","properties":{"name":{"type":"string","enum":["Harry James Potter","Hermione Jean Granger","Ron Weasley","Fred Weasley","George Weasley","Bill Weasley","Percy Weasley","Charlie Weasley","Ginny Weasley","Molly Weasley","Arthur Weasley","Neville Longbottom","Luna Lovegood","Draco Malfoy","Albus Percival Wulfric Brian Dumbledore","Minerva McGonagall","Remus Lupin","Rubeus Hagrid","Sirius Black","Severus Snape","Bellatrix Lestrange","Lord Voldemort","Cedric Diggory","Nymphadora Tonks","James Potter"],"description":"Name of the Harry Potter character"}},"required":["name"]}}},{"type":"function","function":{"name":"name_a_color","description":"A tool that returns a bunch of color names for a given color_hex.","parameters":{"type":"object","properties":{"color_hex":{"type":"string","description":"A hexadecimal color value which must be represented as a string."}},"required":["color_hex"]}}}]
payload["tool_choice"] = "auto"

response = requests.post(invoke_url, headers=headers, json=payload, stream=stream)
if stream:
    for line in response.iter_lines():
        if line:
            print(line.decode("utf-8"))
else:
    print(response.json())
==
new model nvidia: https://build.nvidia.com/google/gemma-4-31b-it
API ref: https://docs.api.nvidia.com/nim/reference/google-gemma-4-31b-it
Python:
import requests, base64

invoke_url = "https://integrate.api.nvidia.com/v1/chat/completions"
stream = True

def read_b64(path):
  with open(path, "rb") as f:
    return base64.b64encode(f.read()).decode()

headers = {
  "Authorization": "Bearer $NVIDIA_API_KEY",
  "Accept": "text/event-stream" if stream else "application/json"
}

payload = {
  "model": "google/gemma-4-31b-it",
  "messages": [{"role":"user","content":""}],
  "max_tokens": 16384,
  "temperature": 1.00,
  "top_p": 0.95,
  "stream": stream,
  "chat_template_kwargs": {"enable_thinking":True},
}
payload["tools"] = [{"type":"function","function":{"name":"describe_harry_potter_character","description":"Returns information and images of Harry Potter characters.","parameters":{"type":"object","properties":{"name":{"type":"string","enum":["Harry James Potter","Hermione Jean Granger","Ron Weasley","Fred Weasley","George Weasley","Bill Weasley","Percy Weasley","Charlie Weasley","Ginny Weasley","Molly Weasley","Arthur Weasley","Neville Longbottom","Luna Lovegood","Draco Malfoy","Albus Percival Wulfric Brian Dumbledore","Minerva McGonagall","Remus Lupin","Rubeus Hagrid","Sirius Black","Severus Snape","Bellatrix Lestrange","Lord Voldemort","Cedric Diggory","Nymphadora Tonks","James Potter"],"description":"Name of the Harry Potter character"}},"required":["name"]}}},{"type":"function","function":{"name":"name_a_color","description":"A tool that returns a bunch of color names for a given color_hex.","parameters":{"type":"object","properties":{"color_hex":{"type":"string","description":"A hexadecimal color value which must be represented as a string."}},"required":["color_hex"]}}}]
payload["tool_choice"] = "auto"

response = requests.post(invoke_url, headers=headers, json=payload, stream=stream)
if stream:
    for line in response.iter_lines():
        if line:
            print(line.decode("utf-8"))
else:
    print(response.json())
==
CLOUDFARE
==
Remove @cf/meta/llama-3.1-8b-instruct-fast
for each of the below models, visit the URL and check under usage (python) and paramterers in the cloudfare page; i can copy paste if you find difficulty in doing so.
add https://developers.cloudflare.com/workers-ai/models/deepseek-r1-distill-qwen-32b/ 
https://developers.cloudflare.com/workers-ai/models/gpt-oss-120b/
https://developers.cloudflare.com/workers-ai/models/kimi-k2.6/
https://developers.cloudflare.com/workers-ai/models/nemotron-3-120b-a12b/
==
GITHUB models - note, github models dont have an explicit thinking, but some models like phi-4 stream thinking on the chat interface directly and dont hide it.
==
remove "openai/gpt-4o-mini",
remove openai/gpt-5
==
https://github.com/marketplace/models/azureml-meta/Llama-3-3-70B-Instruct:
Below are example code snippets for a few use cases. For additional information about Microsoft Foundry Inference SDK, see full documentation and samples.

1. Configure authentication
To use GitHub Models in your codebase, you first need to choose a provider and configure the authentication method. Select a provider below to continue:


GitHub Get started for free
Free access with simple rate limits. Enable billing for higher limits with paid models usage.

Your organization can also set up external models providers by adding API keys.


Microsoft Foundry Pay as you go
Connect your Azure subscription to access models. Get Microsoft Foundry key.

You must give models:read permissions to the token or it will return unauthorized. Note that the token will be sent to a Microsoft service.

If you have external models set up, they are also used by creating a personal access token. To use external models, reference them with custom/key_id/model_id as the model name.

To use the code snippets below, create an environment variable to set your token as the key for the client code.

If you're using bash:

export GITHUB_TOKEN="<your-github-token-goes-here>"
If you're in powershell:

$Env:GITHUB_TOKEN="<your-github-token-goes-here>"
If you're using Windows command prompt:

set GITHUB_TOKEN=<your-github-token-goes-here>
2. Install dependencies
Install the Microsoft Foundry Inference SDK using pip (Requires: Python >=3.8):

pip install azure-ai-inference
3. Run a basic code sample
This sample demonstrates a basic call to the chat completion API. It is leveraging the GitHub AI model inference endpoint and your GitHub token. The call is synchronous.

import os
from azure.ai.inference import ChatCompletionsClient
from azure.ai.inference.models import SystemMessage, UserMessage
from azure.core.credentials import AzureKeyCredential

endpoint = "https://models.github.ai/inference"
model_name = "meta/Llama-3.3-70B-Instruct"
token = os.environ["GITHUB_TOKEN"]

client = ChatCompletionsClient(
    endpoint=endpoint,
    credential=AzureKeyCredential(token),
)

response = client.complete(
    messages=[
        SystemMessage("You are a helpful assistant."),
        UserMessage("What is the capital of France?"),
    ],
    temperature=1.0,
    top_p=1.0,
    max_tokens=1000,
    model=model_name
)

print(response.choices[0].message.content)
4. Explore more samples
Run a multi-turn conversation
This sample demonstrates a multi-turn conversation with the chat completion API. When using the model for a chat application, you'll need to manage the history of that conversation and send the latest messages to the model.

import os
from azure.ai.inference import ChatCompletionsClient
from azure.ai.inference.models import AssistantMessage, SystemMessage, UserMessage
from azure.core.credentials import AzureKeyCredential

token = os.environ["GITHUB_TOKEN"]
endpoint = "https://models.github.ai/inference"
model_name = "meta/Llama-3.3-70B-Instruct"

client = ChatCompletionsClient(
    endpoint=endpoint,
    credential=AzureKeyCredential(token),
)

messages = [
    SystemMessage("You are a helpful assistant."),
    UserMessage("What is the capital of France?"),
    AssistantMessage("The capital of France is Paris."),
    UserMessage("What about Spain?"),
]

response = client.complete(messages=messages, model=model_name)

print(response.choices[0].message.content)
Stream the output
For a better user experience, you will want to stream the response of the model so that the first token shows up early and you avoid waiting for long responses.

import os
from azure.ai.inference import ChatCompletionsClient
from azure.ai.inference.models import SystemMessage, UserMessage
from azure.core.credentials import AzureKeyCredential

token = os.environ["GITHUB_TOKEN"]
endpoint = "https://models.github.ai/inference"
model_name = "meta/Llama-3.3-70B-Instruct"

client = ChatCompletionsClient(
    endpoint=endpoint,
    credential=AzureKeyCredential(token),
)

response = client.complete(
    stream=True,
    messages=[
        SystemMessage("You are a helpful assistant."),
        UserMessage("Give me 5 good reasons why I should exercise every day."),
    ],
    model_extras = {'stream_options': {'include_usage': True}},
    model=model_name,
)

usage = {}
for update in response:
    if update.choices and update.choices[0].delta:
        print(update.choices[0].delta.content or "", end="")
    if update.usage:
        usage = update.usage

if usage:
    print("\n")
    for k, v in usage.items():
        print(f"{k} = {v}")


client.close()
Identify and invoke tools
A language model can be given a set of tools it can invoke, for running specific actions depending on the context of the conversation. This sample demonstrates how to define a function tool and how to act on a request from the model to invoke it.

import os
import json
from azure.ai.inference import ChatCompletionsClient
from azure.ai.inference.models import (
    AssistantMessage,
    ChatCompletionsToolCall,
    ChatCompletionsToolDefinition,
    CompletionsFinishReason,
    FunctionDefinition,
    SystemMessage,
    ToolMessage,
    UserMessage,
)
from azure.core.credentials import AzureKeyCredential

token = os.environ["GITHUB_TOKEN"]
endpoint = "https://models.github.ai/inference"
model_name = "meta/Llama-3.3-70B-Instruct"

# Define a function that returns flight information between two cities (mock implementation)
def get_flight_info(origin_city: str, destination_city: str):
    if origin_city == "Seattle" and destination_city == "Miami":
        return json.dumps({
            "airline": "Delta",
            "flight_number": "DL123",
            "flight_date": "May 7th, 2024",
            "flight_time": "10:00AM"})
    return json.dumps({"error": "No flights found between the cities"})

# Define a function tool that the model can ask to invoke in order to retrieve flight information
flight_info = ChatCompletionsToolDefinition(
    function=FunctionDefinition(
        name="get_flight_info",
        description="""Returns information about the next flight between two cities.
            This includes the name of the airline, flight number and the date and
            time of the next flight""",
        parameters={
            "type": "object",
            "properties": {
                "origin_city": {
                    "type": "string",
                    "description": "The name of the city where the flight originates",
                },
                "destination_city": {
                    "type": "string",
                    "description": "The flight destination city",
                },
            },
            "required": ["origin_city", "destination_city"],
        },
    )
)

client = ChatCompletionsClient(
    endpoint=endpoint,
    credential=AzureKeyCredential(token),
)

messages = [
    SystemMessage("You are an assistant that helps users find flight information."),
    UserMessage("I'm interested in going to Miami. What is the next flight there from Seattle?"),
]

response = client.complete(
    messages=messages,
    tools=[flight_info],
    model=model_name,
)

# We expect the model to ask for a tool call
if response.choices[0].finish_reason == CompletionsFinishReason.TOOL_CALLS:

    # Append the model response to the chat history
    messages.append(AssistantMessage(tool_calls=response.choices[0].message.tool_calls))

    # We expect a single tool call
    if response.choices[0].message.tool_calls and len(response.choices[0].message.tool_calls) == 1:

        tool_call = response.choices[0].message.tool_calls[0]

        # We expect the tool to be a function call
        if isinstance(tool_call, ChatCompletionsToolCall):

            # Parse the function call arguments and call the function
            function_args = json.loads(tool_call.function.arguments.replace("'", '"'))
            print(f"Calling function `{tool_call.function.name}` with arguments {function_args}")
            callable_func = locals()[tool_call.function.name]
            function_return = callable_func(**function_args)
            print(f"Function returned = {function_return}")

            # Append the function call result fo the chat history
            messages.append(ToolMessage(tool_call_id=tool_call.id, content=function_return))

            # Get another response from the model
            response = client.complete(
                messages=messages,
                tools=[flight_info],
                model=model_name,
            )

            print(f"Model response = {response.choices[0].message.content}")
5. Going beyond rate limits
You're using GitHub Models for free with rate limits.
To remove limits and scale your app, enable paid usage. You'll be billed per token used. Learn more about billing.
Using Azure Metered Billing? Go through our bring your own key (BYOK) setup to add your billing info and start using paid model usage through Azure.
==
https://github.com/marketplace/models/azureml-deepseek/DeepSeek-V3-0324/playground/code
Python:
import os
from azure.ai.inference import ChatCompletionsClient
from azure.ai.inference.models import SystemMessage, UserMessage
from azure.core.credentials import AzureKeyCredential

endpoint = "https://models.github.ai/inference"
model = "deepseek/DeepSeek-V3-0324"
token = os.environ["GITHUB_TOKEN"]

client = ChatCompletionsClient(
    endpoint=endpoint,
    credential=AzureKeyCredential(token),
)

response = client.complete(
    messages=[
        SystemMessage(""),
        UserMessage("What is the capital of France?"),
    ],
    temperature=0.8,
    top_p=0.1,
    max_tokens=2048,
    model=model
)

print(response.choices[0].message.content)
==
https://github.com/marketplace/models/azureml/Phi-4-reasoning:
import os
from azure.ai.inference import ChatCompletionsClient
from azure.ai.inference.models import SystemMessage, UserMessage
from azure.core.credentials import AzureKeyCredential

endpoint = "https://models.github.ai/inference"
model = "microsoft/Phi-4-reasoning"
token = os.environ["GITHUB_TOKEN"]

client = ChatCompletionsClient(
    endpoint=endpoint,
    credential=AzureKeyCredential(token),
)

response = client.complete(
    messages=[
        UserMessage("What is the capital of France?"),
    ],
    temperature=1.0,
    top_p=1.0,
    max_tokens=1000,
    model=model
)

print(response.choices[0].message.content)
==
https://github.com/marketplace/models/azureml-deepseek/DeepSeek-R1-0528:
import os
from azure.ai.inference import ChatCompletionsClient
from azure.ai.inference.models import SystemMessage, UserMessage
from azure.core.credentials import AzureKeyCredential

endpoint = "https://models.github.ai/inference"
model = "deepseek/DeepSeek-R1-0528"
token = os.environ["GITHUB_TOKEN"]

client = ChatCompletionsClient(
    endpoint=endpoint,
    credential=AzureKeyCredential(token),
)

response = client.complete(
    messages=[
        UserMessage("What is the capital of France?"),
    ],
    max_tokens=4096,
    model=model
)

print(response.choices[0].message.content)
==
OPENROUTER
==
NEW MODEL (replaces nvidia/nemotron-3-super-120b-a12b:free): https://openrouter.ai/nvidia/nemotron-3-ultra-550b-a55b:free
import requests
import json

# First API call with reasoning
response = requests.post(
  url="https://openrouter.ai/api/v1/chat/completions",
  headers={
    "Authorization": "Bearer <OPENROUTER_API_KEY>",
    "Content-Type": "application/json",
  },
  data=json.dumps({
    "model": "nvidia/nemotron-3-super-120b-a12b:free",
    "messages": [
        {
          "role": "user",
          "content": "How many r's are in the word 'strawberry'?"
        }
      ],
    "reasoning": {"enabled": True}
  })
)

# Extract the assistant message with reasoning_details
response = response.json()
response = response['choices'][0]['message']

# Preserve the assistant message with reasoning_details
messages = [
  {"role": "user", "content": "How many r's are in the word 'strawberry'?"},
  {
    "role": "assistant",
    "content": response.get('content'),
    "reasoning_details": response.get('reasoning_details')  # Pass back unmodified
  },
  {"role": "user", "content": "Are you sure? Think carefully."}
]

# Second API call - model continues reasoning from where it left off
response2 = requests.post(
  url="https://openrouter.ai/api/v1/chat/completions",
  data=json.dumps({
    "model": "nvidia/nemotron-3-super-120b-a12b:free",
    "messages": messages,  # Includes preserved reasoning_details
    "reasoning": {"enabled": True}
  })
)
==
https://openrouter.ai/moonshotai/kimi-k2.6:free:
import requests
import json

# First API call with reasoning
response = requests.post(
  url="https://openrouter.ai/api/v1/chat/completions",
  headers={
    "Authorization": "Bearer <OPENROUTER_API_KEY>",
    "Content-Type": "application/json",
  },
  data=json.dumps({
    "model": "moonshotai/kimi-k2.6:free",
    "messages": [
        {
          "role": "user",
          "content": "How many r's are in the word 'strawberry'?"
        }
      ],
    "reasoning": {"enabled": True}
  })
)

# Extract the assistant message with reasoning_details
response = response.json()
response = response['choices'][0]['message']

# Preserve the assistant message with reasoning_details
messages = [
  {"role": "user", "content": "How many r's are in the word 'strawberry'?"},
  {
    "role": "assistant",
    "content": response.get('content'),
    "reasoning_details": response.get('reasoning_details')  # Pass back unmodified
  },
  {"role": "user", "content": "Are you sure? Think carefully."}
]

# Second API call - model continues reasoning from where it left off
response2 = requests.post(
  url="https://openrouter.ai/api/v1/chat/completions",
  data=json.dumps({
    "model": "moonshotai/kimi-k2.6:free",
    "messages": messages,  # Includes preserved reasoning_details
    "reasoning": {"enabled": True}
  })
)
==
replace "meta-llama/llama-3.3-70b-instruct:free",
        "qwen/qwen3-next-80b-a3b-instruct:free",
        "microsoft/mai-ds-r1", with:
https://openrouter.ai/google/gemma-4-31b-it:free:
import requests
import json

# First API call with reasoning
response = requests.post(
  url="https://openrouter.ai/api/v1/chat/completions",
  headers={
    "Authorization": "Bearer <OPENROUTER_API_KEY>",
    "Content-Type": "application/json",
  },
  data=json.dumps({
    "model": "poolside/laguna-m.1:free",
    "messages": [
        {
          "role": "user",
          "content": "How many r's are in the word 'strawberry'?"
        }
      ],
    "reasoning": {"enabled": True}
  })
)

# Extract the assistant message with reasoning_details
response = response.json()
response = response['choices'][0]['message']

# Preserve the assistant message with reasoning_details
messages = [
  {"role": "user", "content": "How many r's are in the word 'strawberry'?"},
  {
    "role": "assistant",
    "content": response.get('content'),
    "reasoning_details": response.get('reasoning_details')  # Pass back unmodified
  },
  {"role": "user", "content": "Are you sure? Think carefully."}
]

# Second API call - model continues reasoning from where it left off
response2 = requests.post(
  url="https://openrouter.ai/api/v1/chat/completions",
  data=json.dumps({
    "model": "poolside/laguna-m.1:free",
    "messages": messages,  # Includes preserved reasoning_details
    "reasoning": {"enabled": True}
  })
)
--
==
remove poolside/laguna from oppenrouter.
==
