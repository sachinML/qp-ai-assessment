import os
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"

from docling.document_converter import DocumentConverter

def pdf_to_markdown_direct(in_pdf: str, out_md: str):
    conv = DocumentConverter()
    result = conv.convert(in_pdf)
    md = result.document.export_to_markdown()
    with open(out_md, "w", encoding="utf-8") as f:
        f.write(md)

if __name__ == "__main__":
    pdf_to_markdown_direct(r"E:\git_project\qp-ai-assessment\Red_Hat_Enterprise_Linux-10-Composing_installing_and_managing_RHEL_for_Edge_images-en-US.pdf", "output.md")
