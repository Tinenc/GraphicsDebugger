import asyncio
import json
import httpx
import pytest
from langchain_openai import ChatOpenAI
from replaylab_agent.connectors.openai_compatibility import compatible_chat_model
from replaylab_agent.deep_engine import _langchain_tool
from replaylab_agent.provider_diagnostics import provider_failure_metadata


def test_descriptionless_generated_tool_registers_with_real_langchain():
    async def invoke(value: str):
        return value
    tool = _langchain_tool("third_party_no_description", invoke)
    assert tool.description and asyncio.run(tool.ainvoke({"value": "ok"})) == "ok"


def test_gateway_pool_403_is_sent_once_and_never_rewritten_as_a_format_probe():
    calls = []
    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(403, json={'error': {
            'message': 'All available accounts exhausted', 'type': 'server_error'}})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        model = compatible_chat_model(ChatOpenAI, {'model': 'k3', 'api_key': 'test-key',
            'base_url': 'https://gateway.example', 'max_retries': 0, 'http_client': client})
        with pytest.raises(Exception) as error:
            model.invoke('hello')
    assert calls == ['/chat/completions']
    assert provider_failure_metadata(error.value)['errorCode'] == 'provider-capacity'


def completion():
    return {"id":"chat-test", "object":"chat.completion", "created":1, "model":"exact-model",
            "choices":[{"index":0,"message":{"role":"assistant","content":"connected"},"finish_reason":"stop"}]}


@pytest.mark.parametrize('responses',[False,True])
def test_lookaround_schema_is_portable_on_wire_without_changing_tool_contract(responses):
    pattern=r'^(?!reserved$)[A-Za-z][A-Za-z0-9_-]*$'
    original={'type':'object','properties':{'name':{'type':'string','pattern':pattern},
              'color':{'type':'string','pattern':r'^#[a-f0-9]{6}$'}},'required':['name'],'additionalProperties':False}
    async def invoke(**arguments):return arguments
    tool=_langchain_tool('named_target',invoke,args_schema=original)
    requests=[]
    def handler(request):
        body=json.loads(request.content);requests.append(body)
        schema=body['tools'][0]['parameters']if responses else body['tools'][0]['function']['parameters']
        assert 'pattern'not in schema['properties']['name']
        assert pattern in schema['properties']['name']['description']
        assert schema['properties']['color']['pattern']==r'^#[a-f0-9]{6}$'
        assert schema['required']==['name']and schema['additionalProperties']is False
        if not responses:return httpx.Response(200,json=completion())
        return httpx.Response(200,json={'id':'resp-test','object':'response','created_at':1,
            'model':'exact-model','status':'completed','output':[{'id':'msg-test','type':'message',
            'role':'assistant','status':'completed','content':[{'type':'output_text','text':'connected','annotations':[]}]}]})
    client=httpx.Client(transport=httpx.MockTransport(handler))
    model=compatible_chat_model(ChatOpenAI,dict(model='exact-model',api_key='test-key',base_url='https://gateway.example/v1',
        use_responses_api=responses,max_retries=0,http_client=client))
    assert model.bind_tools([tool]).invoke('inspect name').text=='connected'
    assert len(requests)==1
    assert original['properties']['name']['pattern']==pattern
    assert tool.args_schema['properties']['name']['pattern']==pattern


@pytest.mark.parametrize("asynchronous", [False, True])
def test_root_and_responses_negotiate_to_v1_chat_without_changing_model(asynchronous):
    calls = []
    def handler(request):
        body = json.loads(request.content)
        calls.append((str(request.url),body))
        assert body['model'] == 'exact-model'
        assert request.headers['Authorization'] == 'Bearer test-key'
        if request.url.path != '/gateway/v1/chat/completions':
            return httpx.Response(404,json={"error":{"code":"unsupported_endpoint","message":"responses not supported"}})
        return httpx.Response(200,json=completion())
    transport=httpx.MockTransport(handler)
    model=compatible_chat_model(ChatOpenAI,dict(model='exact-model',api_key='test-key',base_url='https://gateway.example/gateway',
        use_responses_api=True,max_retries=0,http_client=httpx.Client(transport=transport),http_async_client=httpx.AsyncClient(transport=transport)))
    result = asyncio.run(model.ainvoke('ping')) if asynchronous else model.invoke('ping')
    assert result.content == 'connected' and len(calls) <= 4
    previous=len(calls)
    result = asyncio.run(model.ainvoke('ping')) if asynchronous else model.invoke('ping')
    assert len(calls) == previous + 1  # Negotiated route reused by the next request.
    assert all(url.startswith('https://gateway.example/gateway') for url,_ in calls)


@pytest.mark.parametrize('status,code',[ (401,'invalid_api_key'),(403,'permission_denied'),(404,'model_not_found'),(429,'rate_limit_exceeded'),(500,'server_error')])
def test_non_contract_failures_do_not_try_other_routes(status,code):
    calls=[]
    def handler(request):
        calls.append(request.url)
        return httpx.Response(status,json={"error":{"code":code,"message":code}})
    transport=httpx.MockTransport(handler)
    model=compatible_chat_model(ChatOpenAI,dict(model='exact-model',api_key='test-key',base_url='https://gateway.example/v1',
        use_responses_api=True,max_retries=0,http_client=httpx.Client(transport=transport)))
    with pytest.raises(Exception): model.invoke('ping')
    assert len(calls) == 1


def test_optional_rejection_is_repaired_but_tool_contract_is_retained():
    calls=[]
    def handler(request):
        body=json.loads(request.content);calls.append(body)
        assert body['tools'][0]['function']['name']=='inspect'
        if 'temperature' in body:
            return httpx.Response(400,json={'error':{'code':'unsupported_parameter','param':'temperature','message':'unsupported parameter'}})
        return httpx.Response(200,json=completion())
    transport=httpx.MockTransport(handler)
    model=compatible_chat_model(ChatOpenAI,dict(model='exact-model',api_key='test-key',base_url='https://gateway.example/v1',
        temperature=0.1,use_responses_api=False,max_retries=0,http_client=httpx.Client(transport=transport)))
    tool={'type':'function','function':{'name':'inspect','description':'Inspect readonly state','parameters':{'type':'object','properties':{}}}}
    assert model.bind_tools([tool]).invoke('ping').content=='connected'
    assert len(calls)==2 and 'temperature' not in calls[1]


@pytest.mark.parametrize("asynchronous", [False, True])
def test_streaming_negotiation_retains_function_calls(asynchronous):
    calls=[]
    def handler(request):
        body=json.loads(request.content);calls.append((request.url.path,body))
        if request.url.path.endswith('/responses'):
            return httpx.Response(405,json={'error':{'code':'unsupported_endpoint','message':'responses not supported'}})
        assert body['tools'][0]['function']['name']=='inspect'
        chunk={'id':'chat-stream','object':'chat.completion.chunk','created':1,'model':'exact-model',
               'choices':[{'index':0,'delta':{'role':'assistant','tool_calls':[{'index':0,'id':'call-inspect','type':'function',
                 'function':{'name':'inspect','arguments':'{}'}}]},'finish_reason':'tool_calls'}]}
        return httpx.Response(200,headers={'content-type':'text/event-stream'},
            content='data: '+json.dumps(chunk)+'\n\ndata: [DONE]\n\n')
    transport=httpx.MockTransport(handler)
    model=compatible_chat_model(ChatOpenAI,dict(model='exact-model',api_key='test-key',base_url='https://gateway.example/v1',
        use_responses_api=True,max_retries=0,http_client=httpx.Client(transport=transport),http_async_client=httpx.AsyncClient(transport=transport)))
    bound=model.bind_tools([{'type':'function','function':{'name':'inspect','description':'Inspect state',
        'parameters':{'type':'object','properties':{}}}}])
    async def collect(): return [chunk async for chunk in bound.astream('ping')]
    chunks=asyncio.run(collect()) if asynchronous else list(bound.stream('ping'))
    assert any(chunk.tool_calls and chunk.tool_calls[0]['name']=='inspect' for chunk in chunks)
    assert len(calls)==2


def test_permanent_route_failure_stops_and_timeouts_do_not_retry():
    for timeout in (False, True):
        calls=[]
        def handler(request):
            calls.append(request)
            if timeout: raise httpx.ReadTimeout('unconfirmed request',request=request)
            return httpx.Response(404,json={'error':{'code':'unsupported_endpoint','message':'endpoint missing'}})
        model=compatible_chat_model(ChatOpenAI,dict(model='exact-model',api_key='test-key',base_url='https://gateway.example/v1',
            use_responses_api=True,max_retries=0,http_client=httpx.Client(transport=httpx.MockTransport(handler))))
        with pytest.raises(Exception): model.invoke('ping')
        assert len(calls)==(1 if timeout else 4)


def test_failure_after_first_stream_chunk_never_negotiates_again():
    from langchain_core.messages import AIMessageChunk
    from langchain_core.outputs import ChatGenerationChunk
    class StreamingModel(ChatOpenAI):
        def _stream(self,*args,**kwargs):
            yield ChatGenerationChunk(message=AIMessageChunk(content='already delivered'))
            raise httpx.ReadTimeout('stream interrupted')
    model=compatible_chat_model(StreamingModel,dict(model='exact-model',api_key='test-key',base_url='https://gateway.example/v1',max_retries=0))
    stream=model.stream('ping')
    assert next(stream).content=='already delivered'
    with pytest.raises(httpx.ReadTimeout): list(stream)
    assert model._bread_delegate is None
