import os

EXCLUIR = {'venv', 'node_modules', '.git', '__pycache__', 'dist', 'build', 'whisper_models'}

def escrever_estrutura(root_path, arquivo_saida):
    with open(arquivo_saida, 'w', encoding='utf-8') as f:
        basename = os.path.basename(os.path.abspath(root_path))
        f.write(f"+--- {basename}\n")
        def listar(pasta, nivel=4):
            nomes = sorted(os.listdir(pasta))
            for nome in nomes:
                if nome in EXCLUIR:
                    continue
                caminho = os.path.join(pasta, nome)
                indentacao = ' ' * nivel
                if os.path.isdir(caminho):
                    f.write(f"{indentacao}+--- {nome}\n")
                    listar(caminho, nivel + 4)
                else:
                    f.write(f"{indentacao}{nome}\n")
        listar(root_path)

if __name__ == "__main__":
    escrever_estrutura('.', 'estrutura.txt')
    print("Estrutura salva em estrutura.txt!")
