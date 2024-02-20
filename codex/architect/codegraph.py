import ast
import logging

from prisma.models import CodeGraph as CodeGraphDBModel
from prisma.types import CodeGraphCreateInput

from codex.architect.model import CodeGraph, FunctionDef
from codex.common.ai_block import (
    AIBlock,
    Identifiers,
    ValidatedResponse,
    ValidationError,
)

logger = logging.getLogger(__name__)


class CodeGraphVisitor(ast.NodeVisitor):
    def __init__(self):
        self.functions = {}
        self.imports = []

    def visit_Import(self, node):
        for alias in node.names:
            import_line = f"import {alias.name}"
            if alias.asname:
                import_line += f" as {alias.asname}"
            self.imports.append(import_line)
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        for alias in node.names:
            import_line = f"from {node.module} import {alias.name}"
            if alias.asname:
                import_line += f" as {alias.asname}"
            self.imports.append(import_line)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        args = []
        for arg in node.args.args:
            arg_type = ast.unparse(arg.annotation) if arg.annotation else "Unknown"
            args.append(arg_type)
        args_str = ", ".join(args)
        return_type = ast.unparse(node.returns) if node.returns else "Unknown"

        # Extracting the docstring if it exists
        if (
            node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, (ast.Str, ast.Constant))
        ):
            doc_string = node.body[0].value.s  # .s to get the string content
        else:
            doc_string = ""  # Or set a default docstring value if you prefer
        self.functions[node.name] = FunctionDef(
            name=node.name,
            doc_string=doc_string,
            args=args_str,
            return_type=return_type,
            function_template=ast.unparse(node),
        )
        self.generic_visit(node)


class CodeGraphAIBlock(AIBlock):
    prompt_template_name = "codegraph"
    model = "gpt-4-0125-preview"
    langauge = "python"

    def validate(
        self, invoke_params: dict, response: ValidatedResponse
    ) -> ValidatedResponse:
        try:
            text = response.response
            code = text.split("```python")[1].split("```")[0]

            try:
                tree = ast.parse(code)
                visitor = CodeGraphVisitor()
                visitor.visit(tree)
            except Exception as e:
                raise ValidationError(f"Error parsing code: {e}")

            assert (
                invoke_params["function_name"] in visitor.functions
            ), f"Function {invoke_params['function_name']} not found in code"

            functions = visitor.functions.copy()
            del functions[invoke_params["function_name"]]
            response.response = CodeGraph(
                function_name=invoke_params["function_name"],
                api_route_spec=invoke_params["api_route"],
                code_graph=visitor.functions[
                    invoke_params["function_name"]
                ].function_template,
                imports=visitor.imports,
                function_defs=functions,
            )
            return response
        except Exception as e:
            raise ValidationError(f"Error validating response: {e}")

    async def create_item(
        self, ids: Identifiers, validated_response: ValidatedResponse
    ):
        """This is just a temporary that doesnt have a database model"""
        try:
            funciton_defs = []
            for key, value in validated_response.response.function_defs.items():
                funciton_defs.append(
                    {
                        "name": value.name,
                        "doc_string": value.doc_string,
                        "args": value.args,
                        "return_type": value.return_type,
                        "function_template": value.function_template,
                    }
                )

            create_input = CodeGraphCreateInput(
                **{
                    "function_name": validated_response.response.function_name,
                    "apiPath": validated_response.response.api_route_spec.path,
                    "code_graph": validated_response.response.code_graph,
                    "imports": validated_response.response.imports,
                    "functionDefs": {"create": funciton_defs},
                    "routeSpec": {
                        "connect": {"id": validated_response.response.api_route_spec.id}
                    },
                }
            )

            if validated_response.response.api_route_spec.schemas:
                create_input["databaseSchema"] = {
                    "connect": {
                        "id": validated_response.response.api_route_spec.schemas.id
                    }
                }

            cg = await CodeGraphDBModel.prisma().create(data=create_input)
            logger.debug(f"Created CodeGraph: {cg}")
            return cg
        except Exception as e:
            logger.info(f"Error saving code graph: {e}")

    async def update_item(self, query_params: CodeGraphDBModel):  # type: ignore
        funciton_defs = []
        if query_params.functionDefs:
            for value in query_params.functionDefs:
                funciton_defs.append(
                    {
                        "name": value.name,
                        "doc_string": value.doc_string,
                        "args": value.args,
                        "return_type": value.return_type,
                        "function_template": value.function_template,
                    }
                )

        cg = await CodeGraphDBModel.prisma().update(
            where={"id": query_params.id},
            data={
                "function_name": query_params.function_name,
                "apiPath": query_params.apiPath,
                "code_graph": query_params.code_graph,
                "imports": query_params.imports,
                "functionDefs": {"create": funciton_defs},
            },
        )

        return cg

    async def get_item(self, item_id: str):  # type: ignore
        cg = await CodeGraphDBModel.prisma().find_unique(where={"id": item_id})

        return cg

    # type: ignore
    async def delete_item(self, item_id: str):  # type: ignore
        await CodeGraphDBModel.prisma().delete(where={"id": item_id})

    async def list_items(self, item_id: str, page: int, page_size: int):  # type: ignore
        cg = await CodeGraphDBModel.prisma().find_many(
            skip=(page - 1) * page_size, take=page_size
        )

        return cg
