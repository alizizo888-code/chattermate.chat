"""
Copyright 2024-2026 ChatterMate

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

from unittest.mock import MagicMock, Mock, patch
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.agents.chat_agent import ChatAgent


def build_agent(transfer_to_human: bool) -> ChatAgent:
    """A ChatAgent with everything external stubbed, to read the prompt it built."""
    agent_data = Mock()
    agent_data.name = "Support"
    agent_data.jira_enabled = False
    agent_data.transfer_to_human = transfer_to_human
    agent_data.ask_for_rating = False
    agent_data.ticketing_enabled = False
    agent_data.instructions = ["You are a helpful assistant"]

    storage = Mock()
    storage.get_session_state.return_value = {"status": "active"}

    with patch('app.agents.chat_agent.get_db') as mock_get_db, \
         patch('app.agents.chat_agent.JiraRepository') as mock_jira_repo, \
         patch('app.tools.knowledge_search_byagent.KnowledgeSearchByAgent', return_value=Mock()), \
         patch('app.agents.chat_agent.EncryptedPostgresAgentStorage', return_value=storage), \
         patch('app.agents.chat_agent.settings.DATABASE_URL', "mock://test"), \
         patch.dict('os.environ', {}, clear=True):
        mock_get_db.return_value = iter([MagicMock(spec=Session)])
        mock_jira_repo.return_value.get_agent_with_jira_config.return_value = agent_data

        return ChatAgent(
            api_key="test-key",
            model_name="gpt-4",
            model_type="openai",
            org_id=str(uuid4()),
            agent_id=str(uuid4()),
            customer_id=str(uuid4()),
            session_id=str(uuid4()),
        )


def prompt_of(agent: ChatAgent) -> str:
    """The built prompt on one line: the blocks are indented heredocs, so a
    sentence is only contiguous once the wrapping whitespace is collapsed."""
    instructions = agent.agent.instructions
    text = "\n".join(instructions) if isinstance(instructions, list) else instructions
    return " ".join((text or "").split())


class TestTransferToHumanPrompt:
    """The switch is a setting on one agent. Said plainly ("transfer to human is
    disabled"), the model repeats it back as a fact about the business - an agent
    answering questions about its own product then tells customers the product
    cannot hand chats to a human, which is false."""

    def test_disabled_transfer_is_scoped_to_this_chat(self):
        prompt = prompt_of(build_agent(transfer_to_human=False)).lower()

        assert "cannot hand this conversation to a human" in prompt
        # It must say what this is NOT, or the model generalises it
        assert "not a fact about the business or its product" in prompt
        # and leave the customer somewhere to go
        assert "open a ticket" in prompt
        # Capability questions are answered from the knowledge base, not the switch
        assert "answer from the knowledge base, not from this setting" in prompt

    def test_enabled_transfer_still_offers_the_handover(self):
        prompt = prompt_of(build_agent(transfer_to_human=True)).lower()

        assert "ability to transfer this conversation to a human agent" in prompt
        assert "cannot hand this conversation to a human" not in prompt
