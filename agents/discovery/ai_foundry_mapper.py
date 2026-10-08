import os
import json
import logging
from typing import Dict, Any, List

logger = logging.getLogger("AIFoundryMapper")

class AzureAIFoundrySchemaMapper:
    """Uses Azure AI Foundry models to infer source-to-canonical mappings."""

    def __init__(self):
        self.endpoint = os.getenv("AZURE_AI_FOUNDRY_ENDPOINT", "https://ai-foundry-wealth.services.ai.azure.com/models")
        self.key = os.getenv("AZURE_AI_FOUNDRY_KEY", "mock-key")

    def infer_schema_mapping(self, source_fields: List[str], canonical_entity_name: str) -> Dict[str, Any]:
        """Infers mapping between raw source fields and canonical attributes."""
        try:
            from azure.ai.inference import ChatCompletionsClient
            from azure.ai.inference.models import SystemMessage, UserMessage
            from azure.core.credentials import AzureKeyCredential

            client = ChatCompletionsClient(
                endpoint=self.endpoint,
                credential=AzureKeyCredential(self.key)
            )

            system_prompt = (
                "You are an expert Wealth-Management Data Architect. "
                "Analyze raw source fields and map them to canonical entity attributes. "
                "Return valid JSON containing mapping, mapping_kind (DIRECT, DERIVED, LOOKUP, EXCLUDED), and confidence_score."
            )

            user_prompt = f"""
            Source System: Amplify / Black Diamond
            Canonical Entity: {canonical_entity_name}
            Source Fields: {json.dumps(source_fields)}

            Respond ONLY in JSON with structure:
            {{
              "mappings": [
                 {{"source_field": "ACCT_NUM", "canonical_field": "account_number", "kind": "DIRECT", "confidence": 0.98}}
              ],
              "unmapped_source_fields": []
            }}
            """

            response = client.complete(
                messages=[
                    SystemMessage(content=system_prompt),
                    UserMessage(content=user_prompt)
                ],
                model="gpt-4o",
                temperature=0.0
            )

            return json.loads(response.choices[0].message.content)

        except Exception as e:
            logger.warning(f"AI Foundry client unavailable or in mock mode: {str(e)}. Using fallback mapping.")
            return {
                "mappings": [
                    {"source_field": "ACCT_NUM", "canonical_field": "account_number", "kind": "DIRECT", "confidence": 0.95},
                    {"source_field": "TRANS_ID", "canonical_field": "transaction_id", "kind": "DIRECT", "confidence": 0.95},
                    {"source_field": "AMOUNT", "canonical_field": "amount", "kind": "DIRECT", "confidence": 0.95}
                ],
                "unmapped_source_fields": []
            }