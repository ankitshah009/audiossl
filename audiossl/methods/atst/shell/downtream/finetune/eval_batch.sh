
source eval_env.sh

DEBUG=0
export DEVICE=5
export NPROC=1
bash eval_spcv2.sh $1 
bash eval_audioset_b.sh $1 
bash eval_voxceleb1.sh $1 
wait
