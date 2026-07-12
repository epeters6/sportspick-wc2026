import unittest
import os
import shutil
import tempfile
import sys
import zipfile

# Add project root to sys.path so we can import scripts
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

class TestImportQuantArtifact(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.project_root = self.test_dir
        
        # We need to monkeypatch the script's root_dir logic 
        # so we don't mess up the actual developer's workspace
        self.original_dirname = os.path.dirname
        def mock_dirname(path):
            if path.endswith('import_quant_artifact.py'):
                return os.path.join(self.project_root, 'scripts')
            if path == os.path.join(self.project_root, 'scripts'):
                return self.project_root
            return self.original_dirname(path)
        os.path.dirname = mock_dirname
        
        os.makedirs(os.path.join(self.project_root, "scripts"), exist_ok=True)
        # We will import the main function from the script dynamically
        
    def tearDown(self):
        os.path.dirname = self.original_dirname
        shutil.rmtree(self.test_dir)
        
    def test_import_quant_artifact_handles_missing_optional_files(self):
        import scripts.import_quant_artifact as importer
        
        # Create a mock source directory with only 1 file
        source_dir = os.path.join(self.test_dir, "mock_source")
        os.makedirs(source_dir)
        with open(os.path.join(source_dir, "sync_status.json"), "w") as f:
            f.write('{"status": "ok"}')
            
        importer.main(source_dir)
        
        self.assertTrue(os.path.exists(os.path.join(self.project_root, "sync_status.json")))
        self.assertFalse(os.path.exists(os.path.join(self.project_root, "sports_shadow_decisions.jsonl")))
        
    def test_import_quant_artifact_backs_up_existing_files(self):
        import scripts.import_quant_artifact as importer
        
        # Create an existing local file
        with open(os.path.join(self.project_root, "sync_status.json"), "w") as f:
            f.write('{"old": "data"}')
            
        source_dir = os.path.join(self.test_dir, "mock_source")
        os.makedirs(source_dir)
        with open(os.path.join(source_dir, "sync_status.json"), "w") as f:
            f.write('{"new": "data"}')
            
        importer.main(source_dir)
        
        # Check that backup exists
        backup_base = os.path.join(self.project_root, "validation_backups")
        self.assertTrue(os.path.exists(backup_base))
        backups = os.listdir(backup_base)
        # Find the timestamp directory
        ts_dir = [d for d in backups if d != 'temp_extract'][0]
        backup_file = os.path.join(backup_base, ts_dir, "sync_status.json")
        self.assertTrue(os.path.exists(backup_file))
        
        with open(backup_file, 'r') as f:
            self.assertEqual(f.read(), '{"old": "data"}')
            
        # Check new file is in place
        with open(os.path.join(self.project_root, "sync_status.json"), 'r') as f:
            self.assertEqual(f.read(), '{"new": "data"}')
            
    def test_import_quant_artifact_from_zip(self):
        import scripts.import_quant_artifact as importer
        
        # Create a zip file
        zip_path = os.path.join(self.test_dir, "artifact.zip")
        with zipfile.ZipFile(zip_path, 'w') as z:
            z.writestr("sports_paper_fills.jsonl", '{"some": "data"}')
            
        importer.main(zip_path)
        
        self.assertTrue(os.path.exists(os.path.join(self.project_root, "sports_paper_fills.jsonl")))
        with open(os.path.join(self.project_root, "sports_paper_fills.jsonl"), 'r') as f:
            self.assertEqual(f.read(), '{"some": "data"}')

if __name__ == "__main__":
    unittest.main()
