set -o errexit

rm -f ~/.condarc
conda create -n sandbox-runtime -y python=3.10

source activate sandbox-runtime

bash ./install-pytorch.sh 2.2.1 12.1.0
pip install "numpy<2.0.0"

conda clean --all -y
