import sys
import types
import unittest
from unittest.mock import Mock, patch

from pathlib import Path
# root = Path(__file__).absolute().parents[1]
# sys.path.append(str(root))


for i, path_tmp in enumerate(list(sys.path)):
    print(f'第{ i }个元素：{ path_tmp }')

# 如果通过-m启动，那么所有运行的py文件中，都是在终端路径开始计算
# （启动时; import时）
# 如果不通过-m，直接运行py文件，
# （启动时，直接运行路径下py文件，import时）
# import Desktop.my_sap_project.src.retriever as retriever
import src.retriever as retriever

class TestGetModel(unittest.TestCase):

    def setUp(self):
        self.original_model = retriever._model
        retriever._model = None

    def tearDown(self):
        retriever._model = self.original_model

    def test_get_model_only_loads_once(self):
        fake_model = object()
        fake_constructor = Mock(return_value=fake_model)

        fake_module = types.ModuleType("sentence_transformers")
        fake_module.SentenceTransformer = fake_constructor

        with patch.dict(
            sys.modules,
            {"sentence_transformers": fake_module},
        ):
            first = retriever._get_model()
            second = retriever._get_model()

        self.assertIs(first, fake_model)
        self.assertIs(second, fake_model)

        fake_constructor.assert_called_once_with(
            retriever.EMBED_MODEL_NAME
        )


if __name__ == "__main__":
    unittest.main()