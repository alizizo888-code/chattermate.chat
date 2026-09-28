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

import pytest

from app.repositories.customer import CustomerRepository


@pytest.fixture
def repo(db):
    return CustomerRepository(db)


class TestIdentify:
    """The shared upsert behind /generate-token and the hosted support chat: an
    embedding app naming someone it already authenticated."""

    def test_creates_an_authenticated_customer(self, repo, test_organization):
        customer = repo.identify(
            email="signed.in@example.com",
            organization_id=test_organization.id,
            full_name="Signed In",
        )

        assert customer.email == "signed.in@example.com"
        assert customer.full_name == "Signed In"
        # What keeps the business's own users out of the People/leads views
        assert customer.is_authenticated is True

    def test_returns_the_same_customer_on_the_next_call(self, repo, test_organization):
        first = repo.identify(email="repeat@example.com", organization_id=test_organization.id)
        second = repo.identify(email="repeat@example.com", organization_id=test_organization.id)

        assert first.id == second.id

    def test_marks_a_previously_anonymous_customer_as_identified(self, repo, test_organization):
        anonymous = repo.create_customer(
            email="was.anonymous@example.com", organization_id=test_organization.id
        )
        assert anonymous.is_authenticated is False

        identified = repo.identify(
            email="was.anonymous@example.com",
            organization_id=test_organization.id,
            full_name="Now Known",
        )

        assert identified.id == anonymous.id
        assert identified.is_authenticated is True
        assert identified.full_name == "Now Known"

    def test_merges_meta_data_and_keeps_untouched_keys(self, repo, test_organization):
        repo.identify(
            email="meta@example.com",
            organization_id=test_organization.id,
            meta_data={"plan": "pro", "seat_count": 3},
        )
        customer = repo.identify(
            email="meta@example.com",
            organization_id=test_organization.id,
            meta_data={"plan": "enterprise"},
        )

        assert customer.meta_data == {"plan": "enterprise", "seat_count": 3}

    def test_keeps_the_stored_name_when_none_is_supplied(self, repo, test_organization):
        repo.identify(
            email="named@example.com",
            organization_id=test_organization.id,
            full_name="Original Name",
        )
        customer = repo.identify(email="named@example.com", organization_id=test_organization.id)

        assert customer.full_name == "Original Name"

    def test_scopes_the_lookup_to_the_organization(self, repo, db, test_organization):
        from app.models.organization import Organization

        other_org = Organization(name="Other Org", domain="other.example")
        db.add(other_org)
        db.commit()
        db.refresh(other_org)

        mine = repo.identify(email="shared@example.com", organization_id=test_organization.id)
        theirs = repo.identify(email="shared@example.com", organization_id=other_org.id)

        assert mine.id != theirs.id

    def test_survives_another_caller_creating_the_same_person_first(
        self, repo, db, test_organization
    ):
        """Two dashboard tabs opening the chat at once both read "no such
        customer" and both insert; the unique constraint fails one of them."""
        winner = repo.create_customer(
            email="race@example.com", organization_id=test_organization.id
        )

        real_lookup = repo.get_customer_by_email
        calls = {"n": 0}

        def lookup_blind_once(email, organization_id):
            # First read happens before the other caller commits, so it sees nothing.
            calls["n"] += 1
            if calls["n"] == 1:
                return None
            return real_lookup(email, organization_id)

        repo.get_customer_by_email = lookup_blind_once

        customer = repo.identify(
            email="race@example.com",
            organization_id=test_organization.id,
            full_name="Raced In",
        )

        assert customer.id == winner.id
        assert customer.is_authenticated is True
        assert customer.full_name == "Raced In"

    def test_does_not_rewrite_meta_data_that_has_not_changed(self, repo, test_organization):
        """The token endpoint calls this on every request; restoring what is
        already stored should not cost a write."""
        meta = {"dashboard_organization_id": "org-1"}
        repo.identify(
            email="steady@example.com", organization_id=test_organization.id, meta_data=meta
        )

        writes = []
        real_update = repo.update_meta_data
        repo.update_meta_data = lambda cid, m: (writes.append(m), real_update(cid, m))[1]

        repo.identify(
            email="steady@example.com", organization_id=test_organization.id, meta_data=meta
        )
        assert writes == []

        # ...but a changed value still lands
        repo.identify(
            email="steady@example.com",
            organization_id=test_organization.id,
            meta_data={"dashboard_organization_id": "org-2"},
        )
        assert writes == [{"dashboard_organization_id": "org-2"}]

    def test_refuses_a_blank_email(self, repo, test_organization):
        with pytest.raises(ValueError):
            repo.identify(email="   ", organization_id=test_organization.id)
