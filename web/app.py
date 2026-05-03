import gradio as gr

with gr.Blocks(title="KB Chatbot") as demo:
    gr.Markdown("# KB Chatbot\nUI coming in Slice 9.")

if __name__ == "__main__":
    demo.launch(server_name="0.0.0.0", server_port=7860)
