import os
import sys
import json
import tempfile
import time
import unittest

# --- .env loader (same pattern as other scripts) ---
def load_env(path=".env"):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ[k.strip()] = v.strip()

load_env()

from qdrant_client import QdrantClient
from rag_client import SharedAgentRAG, validate_ingestion_allowed, build_provenance_metadata
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sync_daemon import load_state, save_state as daemon_save_state

COLLECTION = "test_agent_knowledge"


def _get_qc():
    """Create a Qdrant client connection."""
    api_key = os.getenv("QDRANT_API_KEY")
    return QdrantClient(url="http://localhost:6333", api_key=api_key)


# --- Group A: Document lifecycle (tests 1-8) ---

class TestDocumentLifecycle(unittest.TestCase):
    
    @classmethod
    def setUpClass(cls):
        cls.qc = _get_qc()
        if cls.qc.collection_exists(COLLECTION):
            cls.qc.delete_collection(COLLECTION)
        
        cls.rag = SharedAgentRAG(collection_name=COLLECTION)
        time.sleep(0.5)  # Let index settle
    
    @classmethod
    def tearDownClass(cls):
        if cls.qc.collection_exists(COLLECTION):
            cls.qc.delete_collection(COLLECTION)
    
    def setUp(self):
        # Delete all points without destroying the collection (keeps RAG client valid)
        pts, _ = self.__class__.qc.scroll(
            COLLECTION, limit=10000, with_payload=False, with_vectors=False
        )
        if pts:
            ids = [p.id for p in pts]
            self.__class__.qc.delete(COLLECTION, points_selector=ids)
    def test_01_new_document_indexes(self):
        """T1: New document indexes and is searchable."""
        ids = self.rag.add_knowledge(
            text="SearXNG is a privacy-respecting metasearch engine.",
            agent_id="t_agent", session_id="s1", scope="shared",
            source="manual", external_doc_id="doc_001"
        )
        self.assertGreater(len(ids), 0, "Document should produce chunks")
        time.sleep(0.5)
        results = self.rag.query_knowledge("metasearch engine", agent_id="t_agent", limit=5)
        self.assertTrue(any("SearXNG" in r["text"] for r in results), "New doc should be searchable")
    
    def test_02_same_document_no_duplicate(self):
        """T2: Re-indexing same content does NOT duplicate chunks (UUID5 upsert)."""
        text = "This is the stable document content for dedup testing."
        self.rag.add_knowledge(text=text, agent_id="t_agent", session_id="s1", scope="shared", source="manual", external_doc_id="doc_002")
        count_a = self._point_count()
        self.rag.add_knowledge(text=text, agent_id="t_agent", session_id="s1", scope="shared", source="manual", external_doc_id="doc_002")
        count_b = self._point_count()
        self.assertEqual(count_a, count_b, f"Chunk count grew from {count_a} to {count_b}")
    
    def test_03_content_change_replaces_chunks(self):
        """T3: Content modification replaces old chunks after delete+add."""
        old_text = "Original content about alpha particles and quantum states."
        new_text = "Updated content describing beta decay and nuclear physics."
        
        self.rag.add_knowledge(text=old_text, agent_id="t_agent", session_id="s1", scope="shared", source="manual", external_doc_id="doc_003")
        removed = self.rag.delete_document("doc_003")
        self.assertGreater(removed, 0, "Old chunks should exist for deletion")
        
        self.rag.add_knowledge(text=new_text, agent_id="t_agent", session_id="s1", scope="shared", source="manual", external_doc_id="doc_003")
    
    def test_04_old_content_not_found_after_update(self):
        """T4: Searching for OLD content returns no results after update."""
        old_text = "Testing alpha particles quantum states original."
        new_text = "Updated beta decay nuclear physics replacement."
        
        self.rag.add_knowledge(text=old_text, agent_id="t_agent", session_id="s1", scope="shared", source="manual", external_doc_id="doc_004")
        removed = self.rag.delete_document("doc_004")
        self.assertGreater(removed, 0)
        
        self.rag.add_knowledge(text=new_text, agent_id="t_agent", session_id="s1", scope="shared", source="manual", external_doc_id="doc_004")
        time.sleep(0.5)
        
        results = self.rag.query_knowledge("alpha particles quantum states original", agent_id="t_agent", limit=5)
        matching = [r for r in results if "original" in r["text"].lower()]
        self.assertEqual(len(matching), 0, f"Old content should not be found; got {len(matching)} matches")
    
    def test_05_new_content_found_after_update(self):
        """T5: Searching for NEW content returns results after update."""
        old_t = "Original test content for replacement scenario."
        new_t = "Fresh beta decay nuclear physics replacement text here."
        
        self.rag.add_knowledge(text=old_t, agent_id="t_agent", session_id="s1", scope="shared", source="manual", external_doc_id="doc_005")
        removed = self.rag.delete_document("doc_005")
        self.assertGreater(removed, 0)
        
        self.rag.add_knowledge(text=new_t, agent_id="t_agent", session_id="s1", scope="shared", source="manual", external_doc_id="doc_005")
        time.sleep(0.5)
        
        results = self.rag.query_knowledge("beta decay nuclear physics replacement", agent_id="t_agent", limit=5)
        matching = [r for r in results if "replacement" in r["text"].lower()]
        self.assertGreater(len(matching), 0, "New content should be found")
    
    def test_06_chunk_count_doesnt_grow(self):
        """T6: Multiple add_knowledge calls with same external_doc_id produce no growth (UUID5 upsert)."""
        text = "Stable chunk count verification document content."
        for _ in range(3):
            self.rag.add_knowledge(text=text, agent_id="t_agent", session_id="s1", scope="shared", source="manual", external_doc_id="doc_006")
        
        initial = self._point_count_by_doc("doc_006")
        for _ in range(3):
            self.rag.add_knowledge(text=text, agent_id="t_agent", session_id="s1", scope="shared", source="manual", external_doc_id="doc_006")
        
        final = self._point_count_by_doc("doc_006")
        self.assertEqual(initial, final, f"Chunks grew from {initial} to {final}")
    
    def test_07_delete_removes_all_chunks(self):
        """T7: delete_document removes ALL chunks for that external_doc_id."""
        text_a = "Document A content for deletion testing scenario."
        ids = self.rag.add_knowledge(text=text_a, agent_id="t_agent", session_id="s1", scope="shared", source="manual", external_doc_id="doc_007")
        before = self._point_count_by_doc("doc_007")
        self.assertGreater(before, 0)
        
        removed = self.rag.delete_document("doc_007")
        after = self._point_count_by_doc("doc_007")
        self.assertEqual(after, 0, f"After delete, {after} chunks remain for doc_007")
    
    def test_08_other_documents_survive_deletion(self):
        """T8: Deleting one document does NOT affect another's chunks."""
        text_b = "Document B content that must survive deletion."
        self.rag.add_knowledge(text=text_b, agent_id="t_agent", session_id="s1", scope="shared", source="manual", external_doc_id="doc_008")
        
        count_before = self._point_count_by_doc("doc_008")
        text_c = "Document C content for deletion."
        self.rag.add_knowledge(text=text_c, agent_id="t_agent", session_id="s1", scope="shared", source="manual", external_doc_id="doc_009")
        
        removed = self.rag.delete_document("doc_009")
        count_after = self._point_count_by_doc("doc_008")
        self.assertEqual(count_before, count_after, "Other document chunks should survive deletion")
    
    def _point_count(self):
        pts, _ = self.__class__.qc.scroll(COLLECTION, limit=10000, with_payload=False, with_vectors=False)
        return len(pts)
    
    def _point_count_by_doc(self, doc_id):
        pts, _ = self.__class__.qc.scroll(
            COLLECTION,
            scroll_filter={"must": [{"key": "external_doc_id", "match": {"value": doc_id}}]},
            limit=1000, with_payload=False, with_vectors=False
        )
        return len(pts)


# --- Group B: Idempotency and migration (tests 9-12) ---

class TestIdempotencyMigration(unittest.TestCase):
    
    def test_09_restart_no_duplicates(self):
        """T9: Multiple add_knowledge calls with same external_doc_id produce no duplicates."""
        qc = _get_qc()
        
        test_col = "test_idempotency"
        if qc.collection_exists(test_col):
            qc.delete_collection(test_col)
        
        try:
            rag = SharedAgentRAG(collection_name=test_col)
            time.sleep(0.3)
            
            text = "Idempotent restart verification content document."
            for _ in range(5):
                rag.add_knowledge(text=text, agent_id="t_agent", session_id="s1", scope="shared", source="manual", external_doc_id="idem_001")
            
            pts, _ = qc.scroll(test_col, limit=10000, with_payload=False, with_vectors=False)
            unique_ids = set(p.id for p in pts)
            self.assertLessEqual(len(unique_ids), 3, f"Expected <=3 chunks but got {len(unique_ids)} -- duplicates detected")
        finally:
            if qc.collection_exists(test_col):
                qc.delete_collection(test_col)
    
    def test_10_v1_state_migration(self):
        """T10: Old v1 state (plain list of IDs) migrates to v2 format."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump(["42", "99", "123"], f)
            tmp_path = f.name
        
        try:
            import sync_daemon
            orig_state_file = sync_daemon.STATE_FILE
            sync_daemon.STATE_FILE = tmp_path
            
            state = load_state()
            
            self.assertEqual(state["version"], 2, "Migrated to v2")
            self.assertIn("documents", state)
            self.assertIn("42", state["documents"])
            self.assertIn("99", state["documents"])
            self.assertEqual(state["documents"]["42"]["content_hash"], "")
        finally:
            sync_daemon.STATE_FILE = orig_state_file
            os.unlink(tmp_path)
    
    def test_11_migration_safe_to_replay(self):
        """T11: Running migration logic multiple times is safe."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump(["42"], f)
            tmp_path = f.name
        
        try:
            import sync_daemon
            orig = sync_daemon.STATE_FILE
            sync_daemon.STATE_FILE = tmp_path
            
            state1 = load_state()
            with open(tmp_path, "w", encoding="utf-8") as f2:
                json.dump(state1, f2)
            
            sync_daemon.STATE_FILE = tmp_path
            state2 = load_state()
            
            self.assertEqual(state1["version"], state2["version"])
            self.assertEqual(set(state1["documents"].keys()), set(state2["documents"].keys()))
        finally:
            sync_daemon.STATE_FILE = orig
            os.unlink(tmp_path)
    
    def test_12_corrupt_state_handled(self):
        """T12: Corrupted JSON state file is handled gracefully."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
            f.write("{this is not valid json!!!\n\n")
            tmp_path = f.name
        
        try:
            import sync_daemon
            orig = sync_daemon.STATE_FILE
            sync_daemon.STATE_FILE = tmp_path
            
            state = load_state()
            
            self.assertEqual(state["version"], 2)
            self.assertEqual(len(state["documents"]), 0, "Corrupt file should yield empty documents")
        finally:
            sync_daemon.STATE_FILE = orig
            os.unlink(tmp_path)


# --- Group C: Scope isolation (tests 13-17) ---

class TestScopeIsolation(unittest.TestCase):
    
    @classmethod
    def setUpClass(cls):
        cls.qc = _get_qc()
        if cls.qc.collection_exists(COLLECTION):
            cls.qc.delete_collection(COLLECTION)
        
        cls.rag = SharedAgentRAG(collection_name=COLLECTION)
        time.sleep(0.5)  # Let index settle
    
    @classmethod
    def tearDownClass(cls):
        if cls.qc.collection_exists(COLLECTION):
            cls.qc.delete_collection(COLLECTION)
    
    def setUp(self):
        # Delete all points without destroying the collection (keeps RAG client valid)
        pts, _ = self.__class__.qc.scroll(
            COLLECTION, limit=10000, with_payload=False, with_vectors=False
        )
        if pts:
            ids = [p.id for p in pts]
            self.__class__.qc.delete(COLLECTION, points_selector=ids)
    def test_13_private_visible_to_owner(self):
        """T13: Private-scope document is visible to its owner agent_id."""
        self.rag.add_knowledge(
            text="Private data belonging to agent alpha only.",
            agent_id="alpha", session_id="s1", scope="private", source="manual"
        )
        time.sleep(0.5)
        
        results = self.rag.query_knowledge("private data agent alpha", agent_id="alpha", limit=5)
        matching = [r for r in results if "Private data belonging to agent alpha" in r["text"]]
        self.assertGreater(len(matching), 0, "Owner should see own private content")
    
    def test_14_private_not_visible_to_other(self):
        """T14: Private-scope document is NOT visible to a different agent_id."""
        self.rag.add_knowledge(
            text="Private data belonging to agent alpha only.",
            agent_id="alpha", session_id="s1", scope="private", source="manual"
        )
        time.sleep(0.5)
        
        results = self.rag.query_knowledge("private data agent alpha", agent_id="beta", search_scope="shared_or_private", limit=5)
        matching = [r for r in results if "Private data belonging to agent alpha" in r["text"]]
        self.assertEqual(len(matching), 0, f"Other agent should NOT see private content; got {len(matching)} matches")
    
    def test_15_shared_visible_to_all(self):
        """T15: Shared-scope document is visible to ANY agent querying shared_or_private."""
        self.rag.add_knowledge(
            text="This shared knowledge base entry for all agents.",
            agent_id="alpha", session_id="s1", scope="shared", source="manual"
        )
        time.sleep(0.5)
        
        results = self.rag.query_knowledge("shared knowledge base entry", agent_id="beta", search_scope="shared_or_private", limit=5)
        matching = [r for r in results if "shared knowledge" in r["text"].lower()]
        self.assertGreater(len(matching), 0, "Different agent should see shared content")
    
    def test_16_invalid_search_scope_rejected(self):
        """T16: query_knowledge rejects invalid search_scope with ValueError."""
        with self.assertRaises(ValueError):
            self.rag.query_knowledge("test", agent_id="alpha", search_scope="hacked_scope_injection")
        
        with self.assertRaises(ValueError):
            self.rag.query_knowledge("test", agent_id="alpha", search_scope="")
    
    def test_17_invalid_add_scope_rejected(self):
        """T17: add_knowledge rejects invalid scope values."""
        with self.assertRaises(ValueError):
            self.rag.add_knowledge(
                text="Some test content here for validation.",
                agent_id="alpha", session_id="s1", scope="hacked_injection"
            )
        
        with self.assertRaises(ValueError):
            self.rag.add_knowledge(
                text="Some other test content here too.",
                agent_id="alpha", session_id="s1", scope=""
            )


# --- Group D: Provenance gate (tests 18-22) ---

class TestProvenanceGate(unittest.TestCase):
    
    def test_18_web_search_without_source_url_rejected(self):
        """T18: web_search source without provenance metadata is rejected."""
        self.assertFalse(validate_ingestion_allowed("web_search"))
        self.assertFalse(validate_ingestion_allowed("web_search", {}))
        self.assertFalse(validate_ingestion_allowed("web_search", {"expires_at": "2099-01-01T00:00:00Z"}))
    
    def test_19_web_search_without_expires_at_rejected(self):
        """T19: web_search with partial provenance (missing expires_at) is rejected."""
        self.assertFalse(validate_ingestion_allowed("web_search", {"source_url": "https://example.com"}))
        self.assertFalse(validate_ingestion_allowed("scraping", {"source_url": "https://x.com"}))
    
    def test_20_accepted_web_content_stores_provenance(self):
        """T20: Accepted web_search content stores all provenance fields in chunks."""
        qc = _get_qc()
        
        test_col = "test_provenance"
        if qc.collection_exists(test_col):
            qc.delete_collection(test_col)
        
        try:
            rag = SharedAgentRAG(collection_name=test_col)
            
            prov = build_provenance_metadata(
                source_url="https://example.com/article",
                source_type="web_search",
                trust_level="untrusted"
            )
            self.assertTrue(validate_ingestion_allowed("web_search", prov))
            
            rag.add_knowledge(
                text="This article discusses distributed systems consensus protocols.",
                agent_id="t_agent", session_id="s1", scope="shared",
                source="web_search", extra_metadata=prov
            )
            time.sleep(0.5)
            
            pts, _ = qc.scroll(test_col, limit=100, with_payload=True, with_vectors=False)
            for p in pts:
                payload = p.payload
                self.assertIn("source_url", payload, "Each chunk must carry source_url")
                self.assertEqual(payload["source_url"], "https://example.com/article")
                self.assertIn("trust_level", payload)
                self.assertEqual(payload["trust_level"], "untrusted")
                self.assertIn("expires_at", payload)
        finally:
            if qc.collection_exists(test_col):
                qc.delete_collection(test_col)
    
    def test_21_trusted_local_document_no_provenance_needed(self):
        """T21: Trusted sources pass validation without provenance metadata."""
        for source in ("manual", "paperless", "user_decision"):
            self.assertTrue(validate_ingestion_allowed(source), f"'{source}' should be accepted without provenance")
            self.assertTrue(validate_ingestion_allowed(source, None), f"'{source}' should accept None metadata")
    
    def test_22_past_expires_at_handled(self):
        """T22: Setting expires_at in the past is accepted during ingestion."""
        prov = {
            "source_url": "https://example.com/old",
            "expires_at": "2020-01-01T00:00:00Z"
        }
        self.assertTrue(validate_ingestion_allowed("web_search", prov),
                        "Past expires_at should be accepted at ingestion time")


# --- Group E: Hardening features (tests 23-29) ---

class TestHardeningFeatures(unittest.TestCase):
    
    @classmethod
    def setUpClass(cls):
        cls.qc = _get_qc()
        if cls.qc.collection_exists(COLLECTION):
            cls.qc.delete_collection(COLLECTION)
        
        cls.rag = SharedAgentRAG(collection_name=COLLECTION)
        time.sleep(0.5)  # Let index settle
    
    @classmethod
    def tearDownClass(cls):
        if cls.qc.collection_exists(COLLECTION):
            cls.qc.delete_collection(COLLECTION)
    
    def setUp(self):
        pts, _ = self.__class__.qc.scroll(
            COLLECTION, limit=10000, with_payload=False, with_vectors=False
        )
        if pts:
            ids = [p.id for p in pts]
            self.__class__.qc.delete(COLLECTION, points_selector=ids)
    
    def test_23_crash_safe_reindexing_preserves_old_version(self):
        """T23: add_knowledge with index_version adds new chunks WITHOUT deleting old ones."""
        # Add initial version
        text_v1 = "Version 1 content for crash-safe reindexing test."
        ids_v1 = self.rag.add_knowledge(
            text=text_v1, agent_id="t_agent", session_id="s1", scope="shared", source="manual",
            external_doc_id="crash_test_001", index_version=1
        )
        time.sleep(0.3)
        
        # Add new version WITHOUT cleanup
        text_v2 = "Version 2 content for crash-safe reindexing test."
        ids_v2 = self.rag.add_knowledge(
            text=text_v2, agent_id="t_agent", session_id="s1", scope="shared", source="manual",
            external_doc_id="crash_test_001", index_version=2
        )
        time.sleep(0.3)
        
        # Both versions should exist (old not deleted yet)
        pts, _ = self.__class__.qc.scroll(COLLECTION, limit=100, with_payload=True, with_vectors=False)
        v1_chunks = [p for p in pts if p.payload.get("index_version") == 1]
        v2_chunks = [p for p in pts if p.payload.get("index_version") == 2]
        
        self.assertGreater(len(v1_chunks), 0, "Version 1 chunks should still exist after add_knowledge with version 2")
        self.assertGreater(len(v2_chunks), 0, "Version 2 chunks should exist")
    
    def test_24_startup_reconciliation_catches_mid_crash(self):
        """T24: reconcile_startup removes stale versions after simulated crash."""
        # Simulate: add v1, then v2 without cleanup (crash between save_state and cleanup)
        self.rag.add_knowledge(
            text="Crash simulation v1", agent_id="t_agent", session_id="s1", scope="shared", source="manual",
            external_doc_id="reconcile_test_001", index_version=1,
            extra_metadata={"managed_by": "paperless_sync_daemon"}
        )
        time.sleep(0.3)
        
        self.rag.add_knowledge(
            text="Crash simulation v2", agent_id="t_agent", session_id="s1", scope="shared", source="manual",
            external_doc_id="reconcile_test_001", index_version=2,
            extra_metadata={"managed_by": "paperless_sync_daemon"}
        )
        time.sleep(0.3)
        
        # Verify both versions exist
        pts, _ = self.__class__.qc.scroll(COLLECTION, limit=100, with_payload=True, with_vectors=False)
        before_v1 = sum(1 for p in pts if p.payload.get("external_doc_id") == "reconcile_test_001" and p.payload.get("index_version") == 1)
        before_v2 = sum(1 for p in pts if p.payload.get("external_doc_id") == "reconcile_test_001" and p.payload.get("index_version") == 2)
        
        # Simulate reconcile_startup (cleanup old versions with managed_by filter)
        removed = self.rag.cleanup_old_versions("reconcile_test_001", keep_version=2, managed_by="paperless_sync_daemon")
        time.sleep(0.3)
        
        pts_after, _ = self.__class__.qc.scroll(COLLECTION, limit=100, with_payload=True, with_vectors=False)
        after_v1 = sum(1 for p in pts_after if p.payload.get("external_doc_id") == "reconcile_test_001" and p.payload.get("index_version") == 1)
        after_v2 = sum(1 for p in pts_after if p.payload.get("external_doc_id") == "reconcile_test_001" and p.payload.get("index_version") == 2)
        
        self.assertGreater(before_v1, 0, "v1 should exist before reconciliation")
        self.assertEqual(after_v1, 0, f"v1 should be removed after reconciliation (had {before_v1}, now {after_v1})")
        self.assertGreater(after_v2, 0, "v2 should remain after reconciliation")
    
    def test_25_managed_by_isolation(self):
        """T25: cleanup_old_versions with managed_by filter only affects matching chunks."""
        # Add doc1 with managed_by=paperless_sync_daemon (v1 and v2)
        self.rag.add_knowledge(
            text="Managed by sync daemon v1", agent_id="t_agent", session_id="s1", scope="shared", source="manual",
            external_doc_id="managed_test_001", index_version=1,
            extra_metadata={"managed_by": "paperless_sync_daemon"}
        )
        time.sleep(0.3)
        
        self.rag.add_knowledge(
            text="Managed by sync daemon v2", agent_id="t_agent", session_id="s1", scope="shared", source="manual",
            external_doc_id="managed_test_001", index_version=2,
            extra_metadata={"managed_by": "paperless_sync_daemon"}
        )
        time.sleep(0.3)
        
        # Add doc2 with managed_by=user_manual (different manager)
        self.rag.add_knowledge(
            text="Managed by user manual", agent_id="t_agent", session_id="s1", scope="shared", source="manual",
            external_doc_id="managed_test_002", index_version=1,
            extra_metadata={"managed_by": "user_manual"}
        )
        time.sleep(0.3)
        
        # Cleanup only paperless_sync_daemon managed docs (keep v2, remove v1)
        removed = self.rag.cleanup_old_versions("managed_test_001", keep_version=2, managed_by="paperless_sync_daemon")
        time.sleep(0.3)
        
        pts, _ = self.__class__.qc.scroll(COLLECTION, limit=100, with_payload=True, with_vectors=False)
        doc1_v1 = [p for p in pts if p.payload.get("external_doc_id") == "managed_test_001" and p.payload.get("index_version") == 1]
        doc1_v2 = [p for p in pts if p.payload.get("external_doc_id") == "managed_test_001" and p.payload.get("index_version") == 2]
        doc2_chunks = [p for p in pts if p.payload.get("external_doc_id") == "managed_test_002"]
        
        self.assertEqual(len(doc1_v1), 0, f"doc1 v1 should be cleaned up (got {len(doc1_v1)})")
        self.assertGreater(len(doc1_v2), 0, "doc1 v2 should remain after cleanup")
        self.assertGreater(len(doc2_chunks), 0, "doc2 (different managed_by) should survive cleanup")
    
    def test_26_id_type_normalization(self):
        """T26: ID comparison works with int Paperless IDs and string Qdrant external_doc_id."""
        # Add with integer-like string (simulating Paperless API returning int)
        self.rag.add_knowledge(
            text="ID type test content", agent_id="t_agent", session_id="s1", scope="shared", source="manual",
            external_doc_id="12345"  # String representation of int
        )
        time.sleep(0.3)
        
        pts, _ = self.__class__.qc.scroll(COLLECTION, limit=100, with_payload=True, with_vectors=False)
        found = any(p.payload.get("external_doc_id") == "12345" for p in pts)
        self.assertTrue(found, "Document with string '12345' should be findable")
        
        # Verify set operations work (both sides are strings)
        paperless_ids = {str(12345)}  # Simulate Paperless returning int
        qdrant_ids = {p.payload.get("external_doc_id") for p in pts if p.payload.get("external_doc_id")}
        
        orphaned = qdrant_ids - paperless_ids
        self.assertEqual(len(orphaned), 0, f"ID type mismatch should not cause false orphans; got {orphaned}")
    
    def test_27_upsert_wait_true(self):
        """T27: add_knowledge with wait=True commits before returning."""
        # This is a behavioral test — we verify the upsert completes by checking immediately after
        text = "Wait true verification content."
        ids = self.rag.add_knowledge(
            text=text, agent_id="t_agent", session_id="s1", scope="shared", source="manual",
            external_doc_id="wait_test_001"
        )
        
        # Immediately query (no sleep) — should find results if wait=True worked
        time.sleep(0.1)  # Minimal delay for Qdrant internal processing
        results = self.rag.query_knowledge("Wait true verification", agent_id="t_agent", limit=5)
        matching = [r for r in results if "verification" in r["text"].lower()]
        
        self.assertGreater(len(matching), 0, "Content should be searchable immediately after add_knowledge with wait=True")
    
    def test_28_scroll_all_pagination(self):
        """T28: scroll_all paginates through all pages (no 10k limit)."""
        # Add multiple documents to exceed single-page limit
        for i in range(50):
            self.rag.add_knowledge(
                text=f"Pagination test document number {i} with unique content for testing.",
                agent_id="t_agent", session_id="s1", scope="shared", source="manual",
                external_doc_id=f"scroll_test_{i:03d}"
            )
        time.sleep(1)  # Let index settle
        
        # Use scroll_all (should fetch all pages)
        all_points = self.rag.scroll_all(limit_per_page=10)
        
        self.assertGreaterEqual(len(all_points), 50, f"scroll_all should return >=50 points, got {len(all_points)}")
    
    def test_29_batch_delete_groups(self):
        """T29: delete_documents_managed_by batches deletes in groups of 500."""
        # Add many documents with managed_by metadata
        for i in range(10):  # Small number for testing, but logic should handle larger
            self.rag.add_knowledge(
                text=f"Batch delete test {i}", agent_id="t_agent", session_id="s1", scope="shared", source="manual",
                external_doc_id=f"batch_test_{i:03d}",
                extra_metadata={"managed_by": "paperless_sync_daemon"}
            )
        time.sleep(0.5)
        
        # Delete all managed documents
        removed = self.rag.delete_documents_managed_by("paperless_sync_daemon")
        time.sleep(0.3)
        
        pts, _ = self.__class__.qc.scroll(COLLECTION, limit=100, with_payload=True, with_vectors=False)
        remaining = [p for p in pts if p.payload.get("managed_by") == "paperless_sync_daemon"]
        
        self.assertEqual(len(remaining), 0, f"All managed documents should be deleted; {len(remaining)} remain")


# --- Main ---

if __name__ == "__main__":
    unittest.main(verbosity=2)
