import os
import time
from utils.frase import gerar_prompt_paisagem, gerar_frase_motivacional, gerar_slug
from utils.imagem import gerar_imagem
from utils.video import gerar_video
from utils.tiktok import postar_no_tiktok_e_renomear  # Novo import para upload no TikTok

def main():
    while True:
        # Nova escolha de idioma/país no início
        print("\nEscolha o país para referência da língua das mensagens:")
        print("1. EUA (Inglês) *padrão")
        print("2. Brasil (pt-br)")
        idioma = input("Digite o número da opção (1 ou 2): ")
        if idioma not in ['1', '2']:
            print("Opção inválida! Usando EUA (Inglês) como padrão.")
            idioma = '1'
        idioma = 'en' if idioma == '1' else 'pt-br'

        # Escolha do tipo de postagem
        print("\nEscolha o tipo de postagem:")
        print("1. Vídeo com texto")
        print("2. Foto com texto")
        print("3. Apenas foto")
        tipo_postagem = input("Digite o número da opção (1, 2 ou 3): ")

        # Solicitação de durações
        duracao_video = float(input("Digite a duração do vídeo em segundos (padrão 10): ") or 10)
        duracao_audio = float(input("Digite a duração máxima do áudio em segundos (padrão 30): ") or 30)

        # Geração de frase e imagem
        prompt_paisagem = gerar_prompt_paisagem(idioma)
        frase = gerar_frase_motivacional(idioma)
        slug_paisagem = gerar_slug(prompt_paisagem)
        slug_frase = gerar_slug(frase)
        imagem_path = gerar_imagem(prompt_paisagem, slug_paisagem, frase, slug_frase)

        # Geração de vídeo
        sem_audio = tipo_postagem in ['2', '3']
        video_path = os.path.join("videos", f"{slug_frase}.mp4")
        gerar_video(imagem_path, video_path, duracao_video, duracao_audio, sem_audio)

        # Upload no TikTok
        postar_no_tiktok_e_renomear(video_path, slug_frase, apagar_depois=input("Apagar o vídeo após postagem? (s/n): ").lower() == 's')

        # Aguarda 5 segundos antes de reiniciar
        print("⏳ Aguardando 5 segundos antes de finalizar...")
        time.sleep(5)
        print("✅ Tudo pronto!")

        # Pergunta se deseja continuar
        if input("\nDeseja gerar outro post? (s/n): ").lower() != 's':
            break

if __name__ == "__main__":
    main()