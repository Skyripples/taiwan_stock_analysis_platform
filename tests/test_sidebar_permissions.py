import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SidebarPermissionTests(unittest.TestCase):
    def test_category_links_are_permission_protected(self):
        script = (ROOT / "sidebar.js").read_text(encoding="utf-8")
        self.assertIn('data-category="${group.key}"', script)
        self.assertIn('const protectedCategories = [...sidebar.querySelectorAll(".sidebar-category[data-category]")]', script)
        self.assertIn('required.some((featureKey) => Boolean(permissions[featureKey]))', script)
        self.assertIn('applyItemAccess(item, allowed)', script)
        self.assertIn('event.target.closest(".sidebar-item.is-disabled")', script)
        self.assertIn("event.preventDefault()", script)

    def test_empty_categories_are_admin_only(self):
        script = (ROOT / "sidebar.js").read_text(encoding="utf-8")
        for category in ("futures", "funds", "bonds", "forex", "deposits"):
            self.assertIn(f'{category}: []', script)


if __name__ == "__main__":
    unittest.main()
