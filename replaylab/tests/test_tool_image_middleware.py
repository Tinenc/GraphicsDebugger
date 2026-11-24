"""Chat wire compatibility for vision tool results, including parallel calls."""
import asyncio
import base64
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_openai import ChatOpenAI

from replaylab_agent.tool_media import build_tool_result
from replaylab_agent.tool_image_middleware import chat_tool_image_middleware, project_chat_tool_images


def image_result(value=b'pixels'):
    return build_tool_result('captured EID 1281', [{
        'kind':'image','mediaType':'image/png','base64':base64.b64encode(value).decode(),
        'byteSize':len(value),'name':'view.png'}], 'openai-chat', chat_image_companion=True)


def tool_batch():
    return [HumanMessage('Why is the circled surface blue?'), AIMessage('', tool_calls=[
        {'name':'capture','args':{},'id':'a'}, {'name':'capture','args':{},'id':'b'}]),
        ToolMessage(image_result(), tool_call_id='a', name='capture'),
        ToolMessage(image_result(b'control'), tool_call_id='b', name='capture')]


def test_parallel_tool_images_use_user_image_parts_after_all_tool_results():
    original=tool_batch()
    before=[m.model_dump() for m in original]
    messages=project_chat_tool_images(original)
    payload=ChatOpenAI(api_key='test', model='test', use_responses_api=False)._get_request_payload(messages)
    wire=payload['messages']
    assert [m['role'] for m in wire] == ['user','assistant','tool','tool','user']
    assert [m['tool_call_id'] for m in wire if m['role']=='tool'] == ['a','b']
    assert all(isinstance(m['content'],str) for m in wire if m['role']=='tool')
    images=[b for b in wire[-1]['content'] if b['type']=='image_url']
    assert len(images)==2
    assert images[0]['image_url']['url'].endswith(base64.b64encode(b'pixels').decode())
    assert images[1]['image_url']['url'].endswith(base64.b64encode(b'control').decode())
    assert 'not a new user request' in wire[-1]['content'][0]['text']
    assert messages[-1].additional_kwargs['tool_call_ids'] == ['a','b']
    assert [m.model_dump() for m in original] == before
    assert [m.model_dump() for m in project_chat_tool_images(messages)] == [m.model_dump() for m in messages]


def test_historical_batches_keep_their_images_before_the_next_assistant_answer():
    messages=tool_batch()+[AIMessage('The tint is in the material.'), HumanMessage('And this patch?')]
    projected=project_chat_tool_images(messages)
    assert projected[4].additional_kwargs['bread_tool_media_reference']
    assert projected[5].content == 'The tint is in the material.'
    assert projected[6].content == 'And this patch?'


@pytest.mark.parametrize('asynchronous',[False,True])
def test_model_middleware_projects_only_the_invocation(asynchronous):
    from langchain.agents.middleware import ModelRequest
    model=ChatOpenAI(api_key='test',model='test')
    original=tool_batch()
    request=ModelRequest(model=model,messages=original)
    middleware=chat_tool_image_middleware()
    seen=[]
    def handler(updated): seen.extend(updated.messages);return 'response'
    async def ahandler(updated):return handler(updated)
    result=(asyncio.run(middleware.awrap_model_call(request,ahandler)) if asynchronous else
            middleware.wrap_model_call(request,handler))
    assert result=='response' and len(seen)==5
    assert request.messages is original and len(original)==4


def test_vision_opt_in_controls_both_transport_and_model_middleware(tmp_path):
    from replaylab_agent.deep_engine import BreadMCPTools, _DeepRun
    from replaylab_agent.profiles import Profile
    for enabled in [False,True]:
        profile=Profile(id='p',preset='custom-openai',base_url='http://localhost/v1',
                        model='m',options={'image_input':enabled})
        request=SimpleNamespace(profile=profile,task_id='test-vision-'+str(enabled))
        session=BreadMCPTools(request,SimpleNamespace(),tmp_path)
        run=_DeepRun.__new__(_DeepRun);run.request=request
        assert session._chat_image_companion is enabled
        assert bool(run._tool_image_middleware()) is enabled
        session._output_store.close()


def test_chat_companion_keeps_existing_byte_budget_and_rejects_documents():
    from replaylab_agent.tool_media import MAX_INLINE_ITEM_BYTES
    media=[{'kind':'image','mediaType':'image/png','base64':'eA==','byteSize':MAX_INLINE_ITEM_BYTES+1},
           {'kind':'document','mediaType':'application/pdf','base64':'eA==','byteSize':1}]
    text=build_tool_result('saved',media,'openai-chat',chat_image_companion=True)
    assert isinstance(text,str) and '2 attachment(s) were produced but not shown' in text
    assert 'over the' in text and 'cannot carry document' in text


def test_agent_tool_roundtrip_sees_images_without_persisting_a_synthetic_user_turn():
    from langchain.agents import create_agent
    from langchain_core.language_models import BaseChatModel
    from langchain_core.outputs import ChatGeneration, ChatResult
    from langchain_core.tools import StructuredTool
    from pydantic import PrivateAttr

    class VisionModel(BaseChatModel):
        _wire: list = PrivateAttr(default_factory=list)

        @property
        def _llm_type(self): return 'vision-wire-test'

        def bind_tools(self, tools, **kwargs): return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            if any(isinstance(m,ToolMessage) for m in messages):
                self._wire.append(ChatOpenAI(api_key='test',model='test')._get_request_payload(messages))
                answer=AIMessage('The color is already in the material output.')
            else:
                answer=AIMessage('',tool_calls=[{'name':'capture','args':{},'id':'capture-1'}])
            return ChatResult(generations=[ChatGeneration(message=answer)])

    def capture():
        """Return a captured view."""
        return image_result()

    model=VisionModel()
    agent=create_agent(model=model,tools=[StructuredTool.from_function(capture)],
                       middleware=[chat_tool_image_middleware()])
    result=agent.invoke({'messages':[HumanMessage('Why is the marked area blue?')]})
    assert len(model._wire)==1
    wire=model._wire[0]['messages']
    assert [m['role'] for m in wire] == ['user','assistant','tool','user']
    assert any(b['type']=='image_url' for b in wire[-1]['content'])
    assert sum(isinstance(m,HumanMessage) for m in result['messages'])==1
